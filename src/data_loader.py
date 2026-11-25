"""Carregamento e validação da planilha da Força Tarefa (abas `Cadastros` e `Metadados`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Union

import pandas as pd

DEFAULT_XLSX = Path(__file__).resolve().parent.parent / "data" / "silvicultura_força_tarefa_MS.xlsx"

COL_ID = "ID SilviRadar"
COL_LAT = "Latitude (°)"
COL_LON = "Longitude (°)"
COL_NOME = "Propriedade / Fazenda"
COL_MUN = "Município"
COL_PORTE = "Porte"
COL_SITUACAO = "Situação IBAMA Consolidada"
COL_EMBARGO = "Embargo IBAMA"

MUNICIPIOS_ESPERADOS = [
    "Três Lagoas", "Ribas do Rio Pardo", "Água Clara", "Brasilândia",
    "Selvíria", "Inocência", "Santa Rita do Pardo", "Bataguassu",
]
SITUACOES = ["Regular", "Com Pendência Cadastral", "Com Alerta de Embargo"]
EMBARGOS = ["Área Embargada", "Nada Consta"]
PORTES = ["Pequeno (≤ 100 ha)", "Médio (100–1.000 ha)", "Grande (> 1.000 ha)"]

# Envelope aproximado de Mato Grosso do Sul (com pequena margem).
MS_BOUNDS = {"lat_min": -24.2, "lat_max": -17.0, "lon_min": -58.3, "lon_max": -50.8}

NUMERIC_FLOAT = [
    COL_LAT, COL_LON, "UTM E (m)", "UTM N (m)", "Área Total do Imóvel (ha)",
    "Área Efetiva Plantada (ha)", "IMA (m³/ha.ano)", "Área de Reserva Legal (ha)", "Área de APP (ha)",
]
NUMERIC_INT = [
    "Código IBGE Município", "Ano de Implantação", "Idade do Povoamento (anos)",
    "Estoque Estimado (m³)", "Registro CTF/APP IBAMA", "Volume Movimentado 12 meses (m³)",
]

# Agrupamento dos 45 campos para a ficha completa da propriedade.
FIELD_GROUPS: dict[str, list[str]] = {
    "Identificação": [
        COL_ID, COL_NOME, "Produtor / Detentor", "Tipo de Pessoa", "CPF/CNPJ (mascarado)",
    ],
    "Localização": [
        "UF", COL_MUN, "Código IBGE Município", "Bioma", COL_LAT, COL_LON,
        "Zona UTM", "UTM E (m)", "UTM N (m)",
    ],
    "Dados florestais": [
        "Área Total do Imóvel (ha)", "Área Efetiva Plantada (ha)", COL_PORTE, "Espécie (grupo)",
        "Espécie Cultivada", "Ano de Implantação", "Idade do Povoamento (anos)", "IMA (m³/ha.ano)",
        "Estoque Estimado (m³)", "Finalidade Principal", "Certificação Florestal",
    ],
    "Movimentação DOF / Sinaflor": [
        "Vínculo DOF / Sinaflor", "Produto Florestal Movimentado", "Volume Movimentado 12 meses (m³)",
    ],
    "Regularidade IBAMA": [
        COL_SITUACAO, COL_EMBARGO, "Detalhe do Embargo / Auto", "Registro CTF/APP IBAMA",
        "Atividade CTF/APP", "Situação Cadastral CTF", "Certificado de Regularidade (CR)", "Validade do CR",
    ],
    "Ambiental (CAR · APP · RL · Licenciamento)": [
        "Código CAR", "Situação CAR", "Situação APP", "Área de APP (ha)", "Situação Reserva Legal",
        "Área de Reserva Legal (ha)", "Licenciamento Ambiental Estadual",
    ],
    "Fontes e atualização": ["Fontes de Origem", "Atualizado em"],
}
REQUIRED_COLUMNS = [c for cols in FIELD_GROUPS.values() for c in cols]


@dataclass
class ValidationReport:
    total: int = 0
    validos: int = 0
    colunas_ausentes: list[str] = field(default_factory=list)
    colunas_extras: list[str] = field(default_factory=list)
    ids_duplicados: list[str] = field(default_factory=list)
    ids_vazios: int = 0
    coords_invalidas: list[str] = field(default_factory=list)
    coords_fora_ms: list[str] = field(default_factory=list)
    nulos_por_coluna: dict[str, int] = field(default_factory=dict)
    municipios_inesperados: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.colunas_ausentes or self.ids_duplicados or self.ids_vazios or self.coords_invalidas)

    def mensagens(self) -> list[tuple[str, str]]:
        """Lista de (nível, mensagem) para exibição na interface."""
        msgs: list[tuple[str, str]] = []
        if self.colunas_ausentes:
            msgs.append(("error", f"Colunas obrigatórias ausentes: {', '.join(self.colunas_ausentes)}"))
        if self.ids_vazios:
            msgs.append(("error", f"{self.ids_vazios} registro(s) sem `{COL_ID}` foram descartados."))
        if self.ids_duplicados:
            msgs.append(("error", f"`{COL_ID}` duplicado(s): {', '.join(self.ids_duplicados[:20])} — mantida a 1ª ocorrência."))
        if self.coords_invalidas:
            msgs.append(("error", f"{len(self.coords_invalidas)} registro(s) com coordenadas ausentes/inválidas (fora do mapa): {', '.join(self.coords_invalidas[:20])}"))
        if self.coords_fora_ms:
            msgs.append(("warning", f"{len(self.coords_fora_ms)} registro(s) com coordenadas fora do envelope de MS: {', '.join(self.coords_fora_ms[:20])}"))
        if self.municipios_inesperados:
            msgs.append(("warning", f"Municípios não previstos: {', '.join(self.municipios_inesperados)}"))
        if self.colunas_extras:
            msgs.append(("info", f"Colunas adicionais (exibidas em 'Outros'): {', '.join(self.colunas_extras)}"))
        nulos = {k: v for k, v in self.nulos_por_coluna.items() if v}
        if nulos:
            msgs.append(("info", "Campos com valores nulos: " + ", ".join(f"{k} ({v})" for k, v in nulos.items())))
        return msgs


@dataclass
class Dataset:
    cadastros: pd.DataFrame
    metadados: dict[str, str]
    aviso: str
    report: ValidationReport


Source = Union[str, Path, IO[bytes]]


def _clean_str(s: pd.Series) -> pd.Series:
    s = s.astype("string").str.strip()
    return s.mask(s.isin(["", "nan", "NaN", "None", "—", "-"]))


def load_metadados(source: Source) -> tuple[dict[str, str], str]:
    """Lê a aba `Metadados` como pares chave → valor e extrai o aviso de base sintética."""
    try:
        raw = pd.read_excel(source, sheet_name="Metadados", header=None)
    except (ValueError, KeyError):
        return {}, ""
    meta: dict[str, str] = {}
    fontes: list[str] = []
    for row in raw.itertuples(index=False):
        vals = [str(v).strip() for v in row if pd.notna(v) and str(v).strip()]
        if not vals:
            continue
        if len(vals) == 1:
            if not meta:
                meta["Título"] = vals[0]
            continue
        if "Aviso" in meta:
            break  # após o aviso vêm apenas tabelas auxiliares de contagem
        key, value = vals[0], " ".join(vals[1:])
        if key == "•":
            fontes.append(value)
        else:
            meta[key] = value
    if fontes:
        meta["Fontes consultadas"] = " | ".join(fontes)
    aviso = meta.get("Aviso", "")
    return meta, aviso


def validate(df: pd.DataFrame) -> ValidationReport:
    rep = ValidationReport(total=len(df))
    rep.colunas_ausentes = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    rep.colunas_extras = [c for c in df.columns if c not in REQUIRED_COLUMNS]
    if COL_ID in df.columns:
        ids = df[COL_ID]
        rep.ids_vazios = int(ids.isna().sum())
        dup = ids[ids.duplicated(keep="first") & ids.notna()]
        rep.ids_duplicados = sorted(dup.astype(str).unique().tolist())
    if COL_LAT in df.columns and COL_LON in df.columns:
        lat, lon = df[COL_LAT], df[COL_LON]
        invalid = lat.isna() | lon.isna() | ~lat.between(-90, 90) | ~lon.between(-180, 180)
        rep.coords_invalidas = df.loc[invalid, COL_ID].astype(str).tolist()
        b = MS_BOUNDS
        fora = ~invalid & ~(lat.between(b["lat_min"], b["lat_max"]) & lon.between(b["lon_min"], b["lon_max"]))
        rep.coords_fora_ms = df.loc[fora, COL_ID].astype(str).tolist()
    if COL_MUN in df.columns:
        rep.municipios_inesperados = sorted(set(df[COL_MUN].dropna()) - set(MUNICIPIOS_ESPERADOS))
    rep.nulos_por_coluna = {c: int(df[c].isna().sum()) for c in df.columns}
    return rep


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza tipos e nulos, sem descartar registros (exceto ID vazio)."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:
        if c in NUMERIC_FLOAT:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        elif c in NUMERIC_INT:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        else:
            df[c] = _clean_str(df[c])
    if "Atualizado em" in df.columns:
        df["Atualizado em"] = df["Atualizado em"].astype("string")
    return df


def load_dataset(source: Source = DEFAULT_XLSX) -> Dataset:
    """Carrega, normaliza e valida a planilha. Lança ValueError se colunas essenciais faltarem."""
    if hasattr(source, "seek"):
        source.seek(0)
    raw = pd.read_excel(source, sheet_name="Cadastros")
    df = prepare(raw)
    rep = validate(df)
    essenciais = {COL_ID, COL_LAT, COL_LON}
    if essenciais & set(rep.colunas_ausentes):
        raise ValueError(f"Planilha inválida — colunas essenciais ausentes: {sorted(essenciais & set(rep.colunas_ausentes))}")
    df = df[df[COL_ID].notna()].drop_duplicates(subset=COL_ID, keep="first")
    df["coord_valida"] = ~df[COL_ID].astype(str).isin(rep.coords_invalidas)
    df = df.reset_index(drop=True)
    rep.validos = int(df["coord_valida"].sum())
    if hasattr(source, "seek"):
        source.seek(0)
    meta, aviso = load_metadados(source)
    if not aviso:
        aviso = ("Base cadastral de referência SINTÉTICA para demonstração: nomes, documentos, códigos CAR "
                 "e registros CTF são fictícios. Para decisões, consulte as fontes oficiais.")
    return Dataset(cadastros=df, metadados=meta, aviso=aviso, report=rep)


def field_groups_for(df: pd.DataFrame) -> dict[str, list[str]]:
    """Grupos da ficha completa, incluindo eventuais colunas extras em 'Outros'."""
    groups = {g: [c for c in cols if c in df.columns] for g, cols in FIELD_GROUPS.items()}
    extras = [c for c in df.columns if c not in REQUIRED_COLUMNS and c != "coord_valida"]
    if extras:
        groups["Outros"] = extras
    return groups
