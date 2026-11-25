"""Estrutura base para a PRÓXIMA ETAPA: divisão de propriedades e rotas por equipes de fiscalização.

Este módulo define o modelo de dados (equipes, atribuições, plano) e o contrato das funções de
divisão. Inclui apenas uma heurística simples de referência (setores angulares a partir da origem)
para que a interface já possa exibir uma prévia; as estratégias definitivas estão marcadas como TODO.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import pandas as pd

from .data_loader import COL_EMBARGO, COL_ID, COL_LAT, COL_LON, COL_SITUACAO
from .routing import Origem


class Estrategia(str, Enum):
    MUNICIPIO_PROXIMIDADE = "Por município + proximidade (18 equipes)"
    SETOR_ANGULAR = "Setores a partir da origem (prévia)"
    MUNICIPIO = "Por município"                       # TODO
    PRIORIDADE_RISCO = "Prioridade de risco"           # TODO
    OTIMIZACAO_ROTAS = "Otimização de rotas (VRP)"     # TODO — ex.: OR-Tools + matriz OSRM /table


@dataclass
class Equipe:
    id: str
    nome: str
    integrantes: list[str] = field(default_factory=list)
    base: Optional[Origem] = None
    viaturas: int = 1
    capacidade_propriedades_dia: int = 4
    limite_km_dia: float = 600.0
    municipios_preferenciais: list[str] = field(default_factory=list)
    numero: int = 0
    municipio: str = ""


@dataclass
class Atribuicao:
    equipe_id: str
    ids_silviradar: list[str] = field(default_factory=list)
    ordem_visita: list[str] = field(default_factory=list)
    distancia_total_km: float = 0.0
    duracao_total_s: float = 0.0
    dias_estimados: float = 0.0
    municipio: str = ""
    percurso_km: float = 0.0          # soma geodésica entre fazendas consecutivas da sequência
    origem_ate_primeira_km: float = 0.0


@dataclass
class PlanoDivisao:
    origem: Origem
    estrategia: Estrategia
    equipes: list[Equipe]
    atribuicoes: list[Atribuicao] = field(default_factory=list)
    criado_em: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    observacoes: str = ""

    def equipe_de(self) -> dict[str, str]:
        """Mapa ID SilviRadar → equipe_id."""
        return {fid: a.equipe_id for a in self.atribuicoes for fid in a.ids_silviradar}

    def to_json(self) -> str:
        d = asdict(self)
        d["estrategia"] = self.estrategia.value
        return json.dumps(d, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Prioridade de fiscalização (insumo para as estratégias futuras)
# ---------------------------------------------------------------------------

PESOS_PRIORIDADE = {
    "embargo": 50,
    "alerta_embargo": 30,
    "pendencia": 15,
    "cr_irregular": 10,
    "ctf_irregular": 10,
    "car_sem_registro": 5,
    "licenca_nao_localizada": 5,
}


def calcular_prioridade(row: pd.Series) -> int:
    """Score 0–100 simples e transparente (ajustável em PESOS_PRIORIDADE)."""
    p = 0
    if row.get(COL_EMBARGO) == "Área Embargada":
        p += PESOS_PRIORIDADE["embargo"]
    sit = row.get(COL_SITUACAO)
    if sit == "Com Alerta de Embargo":
        p += PESOS_PRIORIDADE["alerta_embargo"]
    elif sit == "Com Pendência Cadastral":
        p += PESOS_PRIORIDADE["pendencia"]
    cr = str(row.get("Certificado de Regularidade (CR)", ""))
    if cr.startswith("Vencido") or cr.startswith("Não emitido"):
        p += PESOS_PRIORIDADE["cr_irregular"]
    if str(row.get("Situação Cadastral CTF", "Ativo")) != "Ativo":
        p += PESOS_PRIORIDADE["ctf_irregular"]
    if str(row.get("Situação CAR", "")) == "Sem registro público":
        p += PESOS_PRIORIDADE["car_sem_registro"]
    if str(row.get("Licenciamento Ambiental Estadual", "")).startswith("Não localizado"):
        p += PESOS_PRIORIDADE["licenca_nao_localizada"]
    return min(p, 100)


# ---------------------------------------------------------------------------
# Divisão
# ---------------------------------------------------------------------------

def _bearing(o: Origem, lat: float, lon: float) -> float:
    p1, p2 = math.radians(o.lat), math.radians(lat)
    dl = math.radians(lon - o.lon)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def criar_equipes(n: int, origem: Origem) -> list[Equipe]:
    return [Equipe(id=f"EQ-{i:02d}", nome=f"Equipe {i:02d}", base=origem) for i in range(1, n + 1)]


def dividir_por_setor(df: pd.DataFrame, equipes: list[Equipe], origem: Origem,
                      rotas: Optional[dict[str, dict]] = None) -> PlanoDivisao:
    """Heurística de referência: ordena as fazendas pelo azimute a partir da origem e
    distribui em fatias contíguas de tamanho equilibrado (uma por equipe)."""
    rotas = rotas or {}
    pts = sorted(((r[COL_ID], _bearing(origem, r[COL_LAT], r[COL_LON])) for _, r in df.iterrows()),
                 key=lambda t: t[1])
    n = max(len(equipes), 1)
    tam = math.ceil(len(pts) / n) if pts else 0
    plano = PlanoDivisao(origem=origem, estrategia=Estrategia.SETOR_ANGULAR, equipes=equipes)
    for i, eq in enumerate(equipes):
        ids = [fid for fid, _ in pts[i * tam:(i + 1) * tam]]
        dist = sum(rotas.get(f, {}).get("distancia_km", 0.0) for f in ids)
        dur = sum(rotas.get(f, {}).get("duracao_s", 0.0) for f in ids)
        plano.atribuicoes.append(Atribuicao(
            equipe_id=eq.id, ids_silviradar=ids, ordem_visita=list(ids),
            distancia_total_km=round(dist, 1), duracao_total_s=dur,
            dias_estimados=round(len(ids) / max(eq.capacidade_propriedades_dia, 1), 1),
        ))
    plano.observacoes = ("Prévia: distâncias somam rotas ida (origem→fazenda) individuais; "
                         "não há ainda roteirização encadeada entre fazendas.")
    return plano


def dividir_propriedades(df: pd.DataFrame, equipes: list[Equipe], origem: Origem,
                         estrategia: Estrategia = Estrategia.SETOR_ANGULAR,
                         rotas: Optional[dict[str, dict]] = None) -> PlanoDivisao:
    """Ponto de entrada único para a próxima etapa."""
    if estrategia == Estrategia.MUNICIPIO_PROXIMIDADE:
        return dividir_por_municipio(df, origem, DIVISAO_18_EQUIPES)
    if estrategia == Estrategia.SETOR_ANGULAR:
        return dividir_por_setor(df, equipes, origem, rotas)
    raise NotImplementedError(f"Estratégia '{estrategia.value}' será implementada na próxima etapa.")


# ---------------------------------------------------------------------------
# Divisão oficial: equipes por município, agrupadas por proximidade
# ---------------------------------------------------------------------------

# Ordem = numeração das equipes (Três Lagoas primeiro, depois a sequência dos municípios).
# Cada lista indica o nº de propriedades de cada equipe naquele município.
DIVISAO_18_EQUIPES: list[tuple[str, list[int]]] = [
    ("Três Lagoas", [18, 18, 18, 18, 17]),
    ("Bataguassu", [13]),
    ("Brasilândia", [20, 20, 20]),
    ("Inocência", [16, 15]),
    ("Ribas do Rio Pardo", [21, 20, 20, 20]),
    ("Santa Rita do Pardo", [24]),
    ("Selvíria", [19, 18]),
]

SEM_EQUIPE = "Sem equipe"


def _xy_km(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Projeção equiretangular local (km) — suficiente para agrupar pontos de um município."""
    return (lon * 111.320 * math.cos(math.radians(lat0)), lat * 110.574)


def _d2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def agrupar_com_tamanhos(pts: list[tuple[float, float]], tamanhos: list[int],
                         tentativas: int = 40, semente: int = 2026) -> list[int]:
    """Agrupamento geográfico com tamanhos EXATOS (k-means capacitado).

    1) centros iniciais tipo k-means++; 2) atribuição gulosa respeitando a capacidade de cada grupo;
    3) melhoria local por trocas de pares entre grupos (mantém os tamanhos) e recálculo dos centróides.
    Repete com várias sementes e fica com a solução de menor dispersão (soma dos quadrados)."""
    import random

    n, k = len(pts), len(tamanhos)
    if sum(tamanhos) != n:
        raise ValueError(f"Tamanhos {tamanhos} somam {sum(tamanhos)}, mas há {n} propriedades.")
    if k == 1:
        return [0] * n
    rng = random.Random(semente)
    melhor, melhor_custo = None, float("inf")

    def centroides(lab):
        c = []
        for g in range(k):
            m = [pts[i] for i in range(n) if lab[i] == g]
            c.append((sum(p[0] for p in m) / len(m), sum(p[1] for p in m) / len(m)))
        return c

    for _ in range(tentativas):
        cent = [pts[rng.randrange(n)]]
        while len(cent) < k:
            w = [min(_d2(p, c) for c in cent) for p in pts]
            r, acc = rng.random() * sum(w), 0.0
            for i, wi in enumerate(w):
                acc += wi
                if acc >= r:
                    cent.append(pts[i])
                    break
        ordem_tam = list(range(k))
        rng.shuffle(ordem_tam)
        cap = [tamanhos[j] for j in ordem_tam]
        for _it in range(30):
            pares = sorted((_d2(pts[i], cent[g]), i, g) for i in range(n) for g in range(k))
            lab, livre = [-1] * n, cap[:]
            for _, i, g in pares:
                if lab[i] < 0 and livre[g] > 0:
                    lab[i] = g
                    livre[g] -= 1
            melhorou = True
            while melhorou:
                melhorou = False
                cent = centroides(lab)
                for i in range(n):
                    for j in range(i + 1, n):
                        gi, gj = lab[i], lab[j]
                        if gi == gj:
                            continue
                        delta = (_d2(pts[i], cent[gj]) + _d2(pts[j], cent[gi])
                                 - _d2(pts[i], cent[gi]) - _d2(pts[j], cent[gj]))
                        if delta < -1e-9:
                            lab[i], lab[j] = gj, gi
                            melhorou = True
            novo = centroides(lab)
            if all(_d2(a, b) < 1e-12 for a, b in zip(novo, cent)):
                break
            cent = novo
        custo = sum(_d2(pts[i], cent[lab[i]]) for i in range(n))
        if custo < melhor_custo:
            melhor, melhor_custo = lab[:], custo
    return melhor  # type: ignore[return-value]


def sequenciar(pts: list[tuple[float, float]], inicio: tuple[float, float]) -> list[int]:
    """Ordem de visita: começa pela propriedade mais próxima de `inicio`, segue pelo vizinho mais
    próximo e refina com 2-opt (caminho aberto) — propriedades vizinhas ficam em sequência."""
    n = len(pts)
    if n <= 2:
        return sorted(range(n), key=lambda i: _d2(pts[i], inicio))
    atual = min(range(n), key=lambda i: _d2(pts[i], inicio))
    rota, resto = [atual], set(range(n)) - {atual}
    while resto:
        atual = min(resto, key=lambda i: _d2(pts[i], pts[rota[-1]]))
        rota.append(atual)
        resto.remove(atual)
    d = lambda a, b: math.dist(pts[a], pts[b])  # noqa: E731
    melhorou = True
    while melhorou:
        melhorou = False
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                a, b = rota[i - 1], rota[i]
                c = rota[j]
                e = rota[j + 1] if j + 1 < n else None
                antes = d(a, b) + (d(c, e) if e is not None else 0)
                depois = d(a, c) + (d(b, e) if e is not None else 0)
                if depois < antes - 1e-9:
                    rota[i:j + 1] = reversed(rota[i:j + 1])
                    melhorou = True
    return rota


def dividir_por_municipio(df: pd.DataFrame, origem: Origem,
                          divisao: list[tuple[str, list[int]]] = DIVISAO_18_EQUIPES) -> PlanoDivisao:
    """Divide as propriedades em equipes por município conforme `divisao`.

    Municípios com mais de uma equipe são separados por localização (grupos compactos com os tamanhos
    pedidos). Dentro do município, as equipes são numeradas da mais próxima para a mais distante da
    origem; a numeração geral segue a ordem de `divisao`. Municípios fora de `divisao` ficam sem equipe."""
    from .routing import haversine_km

    equipes: list[Equipe] = []
    atribs: list[Atribuicao] = []
    num = 0
    for municipio, tamanhos in divisao:
        sub = df[df["Município"] == municipio]
        if len(sub) != sum(tamanhos):
            raise ValueError(f"{municipio}: {len(sub)} propriedades na base, mas a divisão prevê {sum(tamanhos)}.")
        ids = sub[COL_ID].tolist()
        ll = list(zip(sub[COL_LAT].astype(float), sub[COL_LON].astype(float)))
        lat0 = sum(p[0] for p in ll) / len(ll)
        xy = [_xy_km(la, lo, lat0) for la, lo in ll]
        o_xy = _xy_km(origem.lat, origem.lon, lat0)
        lab = agrupar_com_tamanhos(xy, tamanhos)
        grupos = []
        for g in sorted(set(lab)):
            idx = [i for i in range(len(ids)) if lab[i] == g]
            cx = sum(xy[i][0] for i in idx) / len(idx)
            cy = sum(xy[i][1] for i in idx) / len(idx)
            grupos.append((math.dist((cx, cy), o_xy), idx))
        grupos.sort(key=lambda t: t[0])
        for _, idx in grupos:
            num += 1
            eq = Equipe(id=f"EQ-{num:02d}", nome=f"Equipe {num:02d}", base=origem, numero=num,
                        municipio=municipio, municipios_preferenciais=[municipio],
                        capacidade_propriedades_dia=4)
            seq_local = sequenciar([xy[i] for i in idx], o_xy)
            ordem = [ids[idx[s]] for s in seq_local]
            coords = [ll[idx[s]] for s in seq_local]
            percurso = sum(haversine_km(*coords[i], *coords[i + 1]) for i in range(len(coords) - 1))
            equipes.append(eq)
            atribs.append(Atribuicao(
                equipe_id=eq.id, ids_silviradar=sorted(ordem), ordem_visita=ordem, municipio=municipio,
                percurso_km=round(percurso, 1),
                origem_ate_primeira_km=round(haversine_km(origem.lat, origem.lon, *coords[0]), 1),
                dias_estimados=round(len(ordem) / eq.capacidade_propriedades_dia, 1),
            ))
    plano = PlanoDivisao(origem=origem, estrategia=Estrategia.MUNICIPIO_PROXIMIDADE, equipes=equipes,
                         atribuicoes=atribs)
    fora = sorted(set(df["Município"].dropna()) - {m for m, _ in divisao})
    plano.observacoes = (
        "Equipes numeradas a partir de Três Lagoas e na sequência dos municípios; dentro de cada município, "
        "da equipe mais próxima para a mais distante da origem. Sequência de visita por vizinho mais próximo "
        "+ 2-opt (distâncias geodésicas)."
        + (f" Municípios sem equipe definida: {', '.join(fora)}." if fora else "")
    )
    return plano


def tabela_equipes(plano: PlanoDivisao, df: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por propriedade: Equipe, Nº, Município, Ordem de visita, distância à anterior."""
    from .routing import haversine_km

    pos = df.set_index(COL_ID)
    linhas = []
    for eq, a in zip(plano.equipes, plano.atribuicoes):
        ant = None
        for k, fid in enumerate(a.ordem_visita, 1):
            r = pos.loc[fid]
            cur = (float(r[COL_LAT]), float(r[COL_LON]))
            linhas.append({
                "Equipe": eq.nome, "Nº Equipe": eq.numero, "Município da Equipe": a.municipio,
                "Ordem de Visita": k, "Total da Equipe": len(a.ordem_visita), COL_ID: fid,
                "Propriedade / Fazenda": r["Propriedade / Fazenda"], COL_LAT: cur[0], COL_LON: cur[1],
                "Dist. da anterior (km, linha reta)": round(haversine_km(*ant, *cur), 1) if ant else None,
            })
            ant = cur
    return pd.DataFrame(linhas)
