import json
from pathlib import Path

import pandas as pd
import pytest
import requests

from src import routing
from src.data_loader import COL_ID, DEFAULT_XLSX, load_dataset, prepare, validate
from src.routing import METODO_FALLBACK, METODO_OSRM, ORIGEM_PADRAO, RouteCache, RouteParams, compute_routes
from src.teams import Estrategia, criar_equipes, dividir_propriedades


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DEFAULT_XLSX)


def test_carrega_411_registros_validos(ds):
    assert len(ds.cadastros) == 411
    assert ds.report.ok
    assert ds.report.validos == 411
    assert ds.cadastros[COL_ID].is_unique
    assert "SINTÉTICA" in ds.aviso
    assert ds.cadastros["Município"].nunique() == 8


def test_validacao_detecta_duplicados_e_coords():
    df = prepare(pd.DataFrame({COL_ID: ["A", "A", "B"], "Latitude (°)": [-20.0, -20.0, None],
                               "Longitude (°)": [-52.0, -52.0, -52.0]}))
    rep = validate(df)
    assert rep.ids_duplicados == ["A"]
    assert rep.coords_invalidas == ["B"]
    assert not rep.ok


def test_polyline_roundtrip():
    pts = [(-20.7849, -51.7007), (-21.0, -52.1234), (-20.5, -51.9)]
    dec = routing.decode_polyline(routing.encode_polyline(pts))
    assert all(abs(a - c) < 1e-5 and abs(b - d) < 1e-5 for (a, b), (c, d) in zip(pts, dec))


class _Resp:
    def __init__(self, payload, code=200):
        self._p, self.status_code = payload, code

    def json(self):
        return self._p


class _OkSession(requests.Session):
    def get(self, url, **kw):
        geom = routing.encode_polyline([(-20.7849, -51.7007), (-21.0, -52.0), (-21.31, -52.50)])
        return _Resp({"code": "Ok", "routes": [{"distance": 120000.0, "duration": 5400.0, "geometry": geom}],
                      "waypoints": [{"distance": 3.0}, {"distance": 1500.0}]})


class _DownSession(requests.Session):
    def get(self, url, **kw):
        raise requests.ConnectionError("blocked")


def test_osrm_ok_com_trecho_de_acesso_e_cache(tmp_path: Path):
    cache = RouteCache(tmp_path / "c.json")
    s = compute_routes([("SR-1", -21.319331, -52.509136)], ORIGEM_PADRAO, cache,
                       RouteParams(delay_s=0), session=_OkSession())
    assert s.osrm == 1
    e = cache.get("SR-1", ORIGEM_PADRAO, -21.319331, -52.509136)
    assert e["metodo"] == METODO_OSRM
    assert e["distancia_km"] == pytest.approx(121.5)  # 120 km + 1,5 km de acesso
    assert e["acesso_km"] == pytest.approx(1.5)
    # persistência + reuso do cache
    cache2 = RouteCache(tmp_path / "c.json")
    s2 = compute_routes([("SR-1", -21.319331, -52.509136)], ORIGEM_PADRAO, cache2,
                        RouteParams(delay_s=0), session=_DownSession())
    assert s2.do_cache == 1 and s2.osrm == 0
    assert "rotas" in json.loads((tmp_path / "c.json").read_text())


def test_fallback_quando_osrm_falha(tmp_path: Path):
    cache = RouteCache(tmp_path / "c.json")
    p = RouteParams(delay_s=0, max_retries=0)
    s = compute_routes([("SR-1", -21.3, -52.5), ("SR-2", -20.6, -51.8)], ORIGEM_PADRAO, cache, p,
                       session=_DownSession())
    assert s.fallback == 2
    e = cache.get("SR-1", ORIGEM_PADRAO, -21.3, -52.5)
    assert e["metodo"] == METODO_FALLBACK
    assert e["distancia_km"] == pytest.approx(e["geodesica_km"] * p.fator_sinuosidade, rel=1e-3)


def test_cache_only_nao_consulta(tmp_path: Path):
    cache = RouteCache(tmp_path / "c.json")
    s = compute_routes([("SR-9", -21.0, -52.0)], ORIGEM_PADRAO, cache, use_cache_only=True,
                       session=_DownSession())
    assert s.sem_cache == 1 and not cache.data


def test_divisao_equipes(ds):
    plano = dividir_propriedades(ds.cadastros, criar_equipes(4, ORIGEM_PADRAO), ORIGEM_PADRAO, Estrategia.SETOR_ANGULAR)
    ids = [i for a in plano.atribuicoes for i in a.ids_silviradar]
    assert sorted(ids) == sorted(ds.cadastros[COL_ID]) and len(plano.atribuicoes) == 4
    with pytest.raises(NotImplementedError):
        dividir_propriedades(ds.cadastros, criar_equipes(2, ORIGEM_PADRAO), ORIGEM_PADRAO, Estrategia.MUNICIPIO)


def test_divisao_18_equipes(ds):
    from src.teams import DIVISAO_18_EQUIPES, dividir_por_municipio

    plano = dividir_por_municipio(ds.cadastros, ORIGEM_PADRAO)
    assert [e.numero for e in plano.equipes] == list(range(1, 19))
    # Três Lagoas = equipes 1–5 e demais municípios na ordem da configuração
    esperado = [m for m, tams in DIVISAO_18_EQUIPES for _ in tams]
    assert [a.municipio for a in plano.atribuicoes] == esperado
    for municipio, tams in DIVISAO_18_EQUIPES:
        obtidos = sorted(len(a.ordem_visita) for a in plano.atribuicoes if a.municipio == municipio)
        assert obtidos == sorted(tams), municipio
    todos = [i for a in plano.atribuicoes for i in a.ordem_visita]
    assert len(todos) == len(set(todos)) == 335
    mun = ds.cadastros.set_index(COL_ID)["Município"]
    for a in plano.atribuicoes:
        assert set(mun[a.ordem_visita]) == {a.municipio}
    # Água Clara não está na divisão informada
    assert not (set(ds.cadastros[ds.cadastros["Município"] == "Água Clara"][COL_ID]) & set(todos))


def test_agrupamento_separa_clusters_obvios():
    from src.teams import agrupar_com_tamanhos, sequenciar

    pts = [(0, i) for i in range(5)] + [(100, i) for i in range(5)]
    lab = agrupar_com_tamanhos(pts, [5, 5])
    assert len(set(lab[:5])) == 1 and len(set(lab[5:])) == 1 and lab[0] != lab[5]
    seq = sequenciar([(0, 4), (0, 0), (0, 2), (0, 1), (0, 3)], inicio=(0, -10))
    assert seq == [1, 3, 2, 4, 0]
