"""Rotas rodoviárias ponto a ponto: origem em Três Lagoas - MS → cada fazenda.

Motor principal: API pública do OSRM (`route/v1/driving`). Em caso de falha, timeout ou
ausência de via, aplica-se fallback geodésico (Haversine × fator de sinuosidade ÷ velocidade média).
Todos os resultados ficam em cache JSON persistente, indexado por `ID SilviRadar` + coordenadas da origem.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
DEFAULT_CACHE_FILE = CACHE_DIR / "rotas_tres_lagoas_fazendas.json"
GEOCODE_CACHE_FILE = CACHE_DIR / "geocoding.json"

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "forca-tarefa-silvicultura-ms/1.0 (painel de fiscalizacao)"

# Centro urbano de Três Lagoas - MS (Praça Ramez Tebet / região central).
TRES_LAGOAS_CENTRO = (-20.7849, -51.7007)

METODO_OSRM = "OSRM"
METODO_FALLBACK = "Estimado (Fallback)"

# Trecho final entre a via mapeada no OSM e o ponto exato da fazenda (estrada vicinal/carreador).
ACESSO_MIN_M = 50.0
ACESSO_VEL_KMH = 25.0


@dataclass(frozen=True)
class Origem:
    nome: str
    lat: float
    lon: float
    tipo: str = "Centro urbano (padrão)"

    @property
    def key(self) -> str:
        return f"{self.lat:.5f},{self.lon:.5f}"


ORIGEM_PADRAO = Origem("Três Lagoas - MS (centro urbano)", *TRES_LAGOAS_CENTRO)


@dataclass
class RouteParams:
    osrm_url: str = OSRM_BASE_URL
    timeout_s: float = 15.0
    max_retries: int = 2
    delay_s: float = 0.4            # pausa entre requisições (respeito ao servidor público)
    fator_sinuosidade: float = 1.35  # distância rodoviária ≈ geodésica × fator
    velocidade_fallback_kmh: float = 60.0


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return "—"
    total_min = int(round(seconds / 60))
    h, m = divmod(total_min, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def encode_polyline(coords: Iterable[tuple[float, float]], precision: int = 5) -> str:
    """Codifica [(lat, lon), ...] no formato Google Encoded Polyline."""
    factor = 10 ** precision
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in coords:
        ilat, ilon = int(round(lat * factor)), int(round(lon * factor))
        for v in (ilat - prev_lat, ilon - prev_lon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode_polyline(s: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decodifica Encoded Polyline em [(lat, lon), ...]."""
    if not s:
        return []
    factor = 10 ** precision
    coords, idx, lat, lon = [], 0, 0, 0
    while idx < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        coords.append((lat / factor, lon / factor))
    return coords


def route_coords(entry: dict, max_points: Optional[int] = None) -> list[tuple[float, float]]:
    """Geometria completa para desenho: origem → via (OSRM) → ponto exato da fazenda."""
    pts = decode_polyline(entry.get("geometria", ""))
    o = (entry["origem_lat"], entry["origem_lon"])
    d = (entry["destino_lat"], entry["destino_lon"])
    if not pts:
        return [o, d]
    if max_points and len(pts) > max_points:
        step = math.ceil(len(pts) / max_points)
        pts = pts[::step] + [pts[-1]]
    if pts[0] != o:
        pts = [o] + pts
    if pts[-1] != d:
        pts = pts + [d]
    return pts


# ---------------------------------------------------------------------------
# Cache persistente
# ---------------------------------------------------------------------------

class RouteCache:
    """Cache JSON {"<ID>@<lat,lon origem>": entry}. Escrita atômica, thread-safe."""

    def __init__(self, path: Path = DEFAULT_CACHE_FILE):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: dict[str, dict] = {}
        self.load()

    @staticmethod
    def make_key(farm_id: str, origem: Origem) -> str:
        return f"{farm_id}@{origem.key}"

    def load(self) -> None:
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = payload.get("rotas", payload) if isinstance(payload, dict) else {}
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "versao": 1,
                "descricao": "Rotas ponto a ponto Três Lagoas - MS → fazendas (OSRM / fallback geodésico)",
                "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "rotas": self.data,
            }
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)

    def get(self, farm_id: str, origem: Origem, dest_lat: float, dest_lon: float) -> Optional[dict]:
        e = self.data.get(self.make_key(farm_id, origem))
        if not e:
            return None
        # Invalida se as coordenadas da fazenda mudaram na planilha.
        if abs(e.get("destino_lat", 0) - dest_lat) > 1e-6 or abs(e.get("destino_lon", 0) - dest_lon) > 1e-6:
            return None
        return e

    def put(self, entry: dict) -> None:
        with self._lock:
            self.data[f"{entry['id']}@{entry['origem_key']}"] = entry

    def for_origin(self, origem: Origem) -> dict[str, dict]:
        suffix = f"@{origem.key}"
        return {k[: -len(suffix)]: v for k, v in self.data.items() if k.endswith(suffix)}

    def clear_origin(self, origem: Origem) -> int:
        suffix = f"@{origem.key}"
        with self._lock:
            keys = [k for k in self.data if k.endswith(suffix)]
            for k in keys:
                del self.data[k]
        return len(keys)


# ---------------------------------------------------------------------------
# Cálculo de rotas
# ---------------------------------------------------------------------------

def _base_entry(farm_id: str, origem: Origem, lat: float, lon: float) -> dict:
    return {
        "id": farm_id,
        "origem_key": origem.key,
        "origem_nome": origem.nome,
        "origem_lat": origem.lat,
        "origem_lon": origem.lon,
        "destino_lat": float(lat),
        "destino_lon": float(lon),
        "geodesica_km": round(haversine_km(origem.lat, origem.lon, lat, lon), 2),
        "calculado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fallback_route(farm_id: str, origem: Origem, lat: float, lon: float,
                   params: RouteParams, motivo: str = "") -> dict:
    e = _base_entry(farm_id, origem, lat, lon)
    dist = e["geodesica_km"] * params.fator_sinuosidade
    e.update({
        "metodo": METODO_FALLBACK,
        "distancia_km": round(dist, 2),
        "duracao_s": round(dist / params.velocidade_fallback_kmh * 3600, 0),
        "acesso_km": 0.0,
        "geometria": "",
        "status": f"Fallback: {motivo}" if motivo else "Fallback",
    })
    return e


def osrm_route(session: requests.Session, farm_id: str, origem: Origem, lat: float, lon: float,
               params: RouteParams) -> dict:
    """Consulta o OSRM. Retorna entry com método OSRM ou lança RuntimeError com o motivo."""
    url = f"{params.osrm_url.rstrip('/')}/route/v1/driving/{origem.lon:.6f},{origem.lat:.6f};{lon:.6f},{lat:.6f}"
    q = {"overview": "full", "geometries": "polyline", "alternatives": "false", "steps": "false"}
    last_err = "erro desconhecido"
    for attempt in range(params.max_retries + 1):
        try:
            r = session.get(url, params=q, timeout=params.timeout_s)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}"
                time.sleep(min(2 ** attempt * 1.5, 8))
                continue
            data = r.json()
        except requests.Timeout:
            last_err = "timeout"
            continue
        except (requests.RequestException, ValueError) as exc:
            last_err = f"{type(exc).__name__}"
            time.sleep(0.5 * (attempt + 1))
            continue
        code = data.get("code")
        if code != "Ok" or not data.get("routes"):
            # NoRoute / NoSegment / InvalidQuery: não adianta repetir.
            raise RuntimeError(f"OSRM {code or r.status_code}: {data.get('message', '')}".strip())
        route = data["routes"][0]
        wps = data.get("waypoints") or [{}, {}]
        snap_o = float(wps[0].get("distance", 0) or 0)
        snap_d = float(wps[-1].get("distance", 0) or 0)
        acesso_m = (snap_o if snap_o > ACESSO_MIN_M else 0) + (snap_d if snap_d > ACESSO_MIN_M else 0)
        dist_km = route["distance"] / 1000 + acesso_m / 1000
        dur_s = route["duration"] + (acesso_m / 1000) / ACESSO_VEL_KMH * 3600
        e = _base_entry(farm_id, origem, lat, lon)
        # Proteção contra respostas incoerentes (ex.: snap em via muito distante).
        if e["geodesica_km"] > 1 and dist_km > e["geodesica_km"] * 4:
            raise RuntimeError(f"rota incoerente ({dist_km:.0f} km vs {e['geodesica_km']:.0f} km em linha reta)")
        e.update({
            "metodo": METODO_OSRM,
            "distancia_km": round(dist_km, 2),
            "duracao_s": round(dur_s, 0),
            "acesso_km": round(acesso_m / 1000, 3),
            "geometria": route.get("geometry", ""),
            "status": "OK" + (f" (acesso não mapeado ≈ {acesso_m/1000:.1f} km)" if acesso_m else ""),
        })
        return e
    raise RuntimeError(f"OSRM indisponível ({last_err})")


def compute_route(session: requests.Session, farm_id: str, origem: Origem, lat: float, lon: float,
                  params: RouteParams) -> dict:
    try:
        return osrm_route(session, farm_id, origem, lat, lon, params)
    except RuntimeError as exc:
        return fallback_route(farm_id, origem, lat, lon, params, str(exc))


@dataclass
class BatchSummary:
    solicitadas: int = 0
    do_cache: int = 0
    osrm: int = 0
    fallback: int = 0
    sem_cache: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def compute_routes(
    farms: Iterable[tuple[str, float, float]],
    origem: Origem,
    cache: RouteCache,
    params: RouteParams = RouteParams(),
    *,
    use_cache_only: bool = False,
    force: bool = False,
    retry_fallback: bool = True,
    progress: Optional[Callable[[int, int, str], None]] = None,
    session: Optional[requests.Session] = None,
    save_every: int = 20,
) -> BatchSummary:
    """Calcula/atualiza rotas para uma lista de (id, lat, lon)."""
    farms = list(farms)
    summ = BatchSummary(solicitadas=len(farms))
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    osrm_down_streak = 0
    dirty = 0
    for i, (fid, lat, lon) in enumerate(farms, 1):
        cached = None if force else cache.get(fid, origem, lat, lon)
        reuse = cached is not None and not (retry_fallback and cached.get("metodo") == METODO_FALLBACK and not use_cache_only)
        if reuse:
            summ.do_cache += 1
        elif use_cache_only:
            summ.sem_cache += 1
        else:
            if osrm_down_streak >= 5:
                # Servidor claramente fora do ar: evita esperar timeout em todas as fazendas.
                entry = fallback_route(fid, origem, lat, lon, params, "OSRM indisponível (falhas consecutivas)")
            else:
                entry = compute_route(sess, fid, origem, lat, lon, params)
                status = entry.get("status", "")
                osrm_down_streak = osrm_down_streak + 1 if "indisponível" in status else 0
                time.sleep(params.delay_s)
            cache.put(entry)
            dirty += 1
            if entry["metodo"] == METODO_OSRM:
                summ.osrm += 1
            else:
                summ.fallback += 1
            if dirty >= save_every:
                cache.save()
                dirty = 0
        if progress:
            progress(i, len(farms), fid)
    if dirty:
        cache.save()
    return summ


# ---------------------------------------------------------------------------
# Geocoding da origem (Nominatim) com cache
# ---------------------------------------------------------------------------

def geocode(query: str = "Três Lagoas, Mato Grosso do Sul, Brasil", timeout_s: float = 10.0) -> Optional[tuple[float, float]]:
    cache: dict = {}
    if GEOCODE_CACHE_FILE.exists():
        try:
            cache = json.loads(GEOCODE_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    if query in cache:
        return tuple(cache[query])  # type: ignore[return-value]
    try:
        r = requests.get(NOMINATIM_URL, params={"q": query, "format": "json", "limit": 1, "countrycodes": "br"},
                         headers={"User-Agent": USER_AGENT}, timeout=timeout_s)
        r.raise_for_status()
        res = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not res:
        return None
    coord = (round(float(res[0]["lat"]), 6), round(float(res[0]["lon"]), 6))
    cache[query] = list(coord)
    GEOCODE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return coord
