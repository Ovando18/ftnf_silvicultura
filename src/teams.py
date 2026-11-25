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


@dataclass
class Atribuicao:
    equipe_id: str
    ids_silviradar: list[str] = field(default_factory=list)
    ordem_visita: list[str] = field(default_factory=list)
    distancia_total_km: float = 0.0
    duracao_total_s: float = 0.0
    dias_estimados: float = 0.0


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
    if estrategia == Estrategia.SETOR_ANGULAR:
        return dividir_por_setor(df, equipes, origem, rotas)
    raise NotImplementedError(f"Estratégia '{estrategia.value}' será implementada na próxima etapa.")
