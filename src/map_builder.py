"""Montagem do mapa Folium: fazendas, origem, rotas ponto a ponto e foco na seleção."""

from __future__ import annotations

import html
from typing import Optional

import folium
import pandas as pd
from branca.element import MacroElement, Template
from folium.plugins import Fullscreen, MeasureControl

from .data_loader import COL_EMBARGO, COL_ID, COL_LAT, COL_LON, COL_MUN, COL_NOME, COL_PORTE, COL_SITUACAO
from .routing import METODO_OSRM, Origem, format_duration, route_coords

SITUACAO_CORES = {
    "Regular": "#2E7D32",
    "Com Pendência Cadastral": "#F9A825",
    "Com Alerta de Embargo": "#C62828",
}
COR_PADRAO = "#607D8B"
COR_EMBARGO_BORDA = "#000000"
COR_ROTA = "#1565C0"
COR_ROTA_DESTAQUE = "#FF6F00"

RAIO_PORTE = {"Pequeno (≤ 100 ha)": 5, "Médio (100–1.000 ha)": 7, "Grande (> 1.000 ha)": 9}

TILES = {
    "OpenStreetMap": ("OpenStreetMap", None),
    "Satélite (Esri)": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics",
    ),
    "Claro (CARTO)": (
        "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        "© OpenStreetMap contributors © CARTO",
    ),
}

TOOLTIP_SEP = " · "

# 18+ cores bem distintas para as equipes (repete ciclicamente se houver mais).
CORES_EQUIPES = [
    "#e6194B", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6", "#9A6324", "#469990",
    "#800000", "#808000", "#000075", "#bfef45", "#fabed4", "#ffe119", "#dcbeff", "#aaffc3", "#a9a9a9",
]
COR_SEM_EQUIPE = "#9E9E9E"


def cor_equipe(numero: Optional[int]) -> str:
    return CORES_EQUIPES[(numero - 1) % len(CORES_EQUIPES)] if numero else COR_SEM_EQUIPE


def tooltip_text(row: pd.Series, equipe: Optional[dict] = None) -> str:
    """Texto do tooltip: começa SEMPRE pelo ID, usado para identificar o clique."""
    t = f"{row[COL_ID]}{TOOLTIP_SEP}{row[COL_NOME]}{TOOLTIP_SEP}{row[COL_MUN]}"
    if equipe:
        t += f"{TOOLTIP_SEP}{equipe['nome']} (visita {equipe['ordem']}/{equipe['total']})"
    return t


def id_from_tooltip(tooltip: Optional[str]) -> Optional[str]:
    if not tooltip:
        return None
    return str(tooltip).split(TOOLTIP_SEP)[0].strip() or None


def _fmt_num(v, dec: int = 1, suf: str = "") -> str:
    if v is None or pd.isna(v):
        return "—"
    s = f"{float(v):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s}{suf}"


def popup_html(row: pd.Series, rota: Optional[dict], equipe: Optional[dict] = None) -> str:
    e = lambda v: html.escape("—" if v is None or pd.isna(v) else str(v))  # noqa: E731
    cor = SITUACAO_CORES.get(row[COL_SITUACAO], COR_PADRAO)
    embargo = row[COL_EMBARGO]
    emb_html = (f"<span style='color:#C62828;font-weight:700'>⚠ {e(embargo)}</span>"
                if embargo == "Área Embargada" else e(embargo))
    rota_html = ""
    if rota:
        metodo = rota["metodo"]
        rota_html = (
            f"<tr><td><b>Rota (de {e(rota.get('origem_nome'))})</b></td>"
            f"<td>{_fmt_num(rota['distancia_km'])} km · {format_duration(rota['duracao_s'])}"
            f"<br><small>{e(metodo)}</small></td></tr>"
        )
    equipe_html = ""
    if equipe:
        equipe_html = (f"<tr><td><b>Equipe</b></td><td><span style='color:{cor_equipe(equipe['numero'])};"
                       f"font-weight:700'>■ {e(equipe['nome'])}</span> · visita {equipe['ordem']} de {equipe['total']}</td></tr>")
    return f"""
<div style="font-family:sans-serif;font-size:12px;min-width:250px">
  <div style="font-size:14px;font-weight:700;margin-bottom:2px">{e(row[COL_NOME])}</div>
  <div style="color:#555;margin-bottom:6px">{e(row[COL_ID])} · {e(row[COL_MUN])}/MS</div>
  <table style="border-collapse:collapse;width:100%">
    <tr><td><b>Situação IBAMA</b></td><td><span style="color:{cor};font-weight:700">● {e(row[COL_SITUACAO])}</span></td></tr>
    <tr><td><b>Embargo</b></td><td>{emb_html}</td></tr>
    <tr><td><b>Porte</b></td><td>{e(row[COL_PORTE])}</td></tr>
    <tr><td><b>Área plantada</b></td><td>{_fmt_num(row.get('Área Efetiva Plantada (ha)'))} ha</td></tr>
    <tr><td><b>Produtor</b></td><td>{e(row.get('Produtor / Detentor'))}</td></tr>
    {equipe_html}
    {rota_html}
  </table>
  <div style="margin-top:6px;color:#1565C0"><i>Ficha completa no painel ao lado →</i></div>
</div>"""


def _legend(legenda_equipes: Optional[dict[int, str]] = None, sem_equipe: int = 0) -> MacroElement:
    aberta = True
    if legenda_equipes is not None:
        aberta = len(legenda_equipes) <= 6
        titulo = "Equipes de fiscalização"
        itens = "<div style='columns:2;column-gap:10px'>" + "".join(
            f"<div><span style='display:inline-block;width:11px;height:11px;border-radius:50%;background:{cor_equipe(n)};"
            f"margin-right:5px'></span>{html.escape(t)}</div>" for n, t in sorted(legenda_equipes.items())
        ) + "</div>"
        if sem_equipe:
            itens += (f"<div><span style='display:inline-block;width:11px;height:11px;border-radius:50%;"
                      f"background:{COR_SEM_EQUIPE};margin-right:5px'></span>Sem equipe ({sem_equipe})</div>")
    else:
        titulo = "Situação IBAMA Consolidada"
        itens = "".join(
            f"<div><span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:{c};margin-right:6px'></span>{s}</div>"
            for s, c in SITUACAO_CORES.items()
        )
    tpl = f"""
{{% macro html(this, kwargs) %}}
<div style="position:fixed;bottom:28px;right:10px;z-index:9999;background:rgba(255,255,255,.93);
            padding:8px 10px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);font:12px sans-serif;color:#222">
  <details {"open" if aberta else ""}><summary style="cursor:pointer"><b>Legenda · {titulo}</b></summary>{itens}
  <div style="margin-top:4px"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;
       border:3px solid #000;margin-right:6px"></span>Área Embargada (borda preta)</div>
  <div><span style="display:inline-block;width:18px;border-top:3px solid {COR_ROTA};margin-right:6px"></span>Rota OSRM</div>
  <div><span style="display:inline-block;width:18px;border-top:3px dashed {COR_ROTA};margin-right:6px"></span>Rota estimada (fallback) / acesso</div>
  <div><span style="display:inline-block;width:18px;border-top:4px solid {COR_ROTA_DESTAQUE};margin-right:6px"></span>Rota da fazenda selecionada</div>
  <div style="margin-top:2px">Tamanho do círculo = porte</div>
  </details>
</div>
{{% endmacro %}}"""
    m = MacroElement()
    m._template = Template(tpl)
    return m


def _draw_route(layer, entry: dict, highlight: bool, max_points: Optional[int]) -> list[tuple[float, float]]:
    pts = route_coords(entry, max_points=None if highlight else max_points)
    is_osrm = entry["metodo"] == METODO_OSRM
    cor = COR_ROTA_DESTAQUE if highlight else COR_ROTA
    tip = f"{entry['id']}: {entry['distancia_km']:.1f} km · {format_duration(entry['duracao_s'])} ({entry['metodo']})"
    if is_osrm and len(pts) > 2:
        folium.PolyLine(pts[:-1], color=cor, weight=6 if highlight else 2.5,
                        opacity=0.95 if highlight else 0.55, tooltip=tip).add_to(layer)
        # Trecho de acesso entre a via mapeada e o ponto exato da fazenda.
        folium.PolyLine(pts[-2:], color=cor, weight=3 if highlight else 1.5, dash_array="4 6",
                        opacity=0.9, tooltip="Acesso estimado (via não mapeada)").add_to(layer)
    else:
        folium.PolyLine(pts, color=cor, weight=5 if highlight else 2, dash_array="8 8",
                        opacity=0.9 if highlight else 0.5, tooltip=tip).add_to(layer)
    return pts


def build_map(
    df: pd.DataFrame,
    origem: Origem,
    rotas: dict[str, dict],
    rotas_visiveis: list[str],
    selected_id: Optional[str] = None,
    tiles: str = "OpenStreetMap",
    max_points_por_rota: int = 400,
    equipes: Optional[dict[str, dict]] = None,
    cor_por_equipe: bool = False,
    sequencias: Optional[list[dict]] = None,
) -> folium.Map:
    """
    df             : fazendas a plotar (já filtradas; somente coordenadas válidas)
    rotas          : {ID: entry de rota} para a origem atual (usado em popups)
    rotas_visiveis : IDs cujas rotas devem ser desenhadas
    selected_id    : fazenda em foco (marcador + rota destacados, zoom automático)
    equipes        : {ID: {"numero", "nome", "ordem", "total"}} — atribuição às equipes
    cor_por_equipe : colore os marcadores pela equipe (senão, pela situação IBAMA)
    sequencias     : [{"numero", "nome", "coords": [(lat, lon), ...]}] — sequência de visita a desenhar
    """
    equipes = equipes or {}
    m = folium.Map(location=[origem.lat, origem.lon], zoom_start=8, tiles=None, control_scale=True,
                   prefer_canvas=True)
    for name, (url, attr) in TILES.items():
        folium.TileLayer(url, name=name, attr=attr, show=(name == tiles)).add_to(m)

    # --- Rotas -------------------------------------------------------------------
    camada_rotas = folium.FeatureGroup(name=f"Rotas ponto a ponto ({len(rotas_visiveis)})", show=True)
    for fid in rotas_visiveis:
        if fid != selected_id and fid in rotas:
            _draw_route(camada_rotas, rotas[fid], highlight=False, max_points=max_points_por_rota)
    camada_rotas.add_to(m)

    # --- Sequência de visita das equipes ---------------------------------------------
    if sequencias:
        camada_seq = folium.FeatureGroup(name=f"Sequência de visita ({len(sequencias)} equipes)", show=True)
        for sq in sequencias:
            cor = cor_equipe(sq["numero"])
            folium.PolyLine(sq["coords"], color=cor, weight=3, opacity=0.85,
                            tooltip=f"{sq['nome']} · {len(sq['coords'])} propriedades").add_to(camada_seq)
            if sq.get("rotulos"):
                for k, (la, lo) in enumerate(sq["coords"], 1):
                    folium.Marker([la, lo], icon=folium.DivIcon(
                        icon_size=(18, 18), icon_anchor=(-4, 20),
                        html=f"<div style='font:700 10px sans-serif;color:#fff;background:{cor};border-radius:8px;"
                             f"padding:0 4px;border:1px solid #fff;white-space:nowrap'>{k}</div>")).add_to(camada_seq)
        camada_seq.add_to(m)

    # --- Fazendas por situação (ou por equipe) -----------------------------------------
    grupos: dict[str, folium.FeatureGroup] = {}
    if cor_por_equipe:
        chave = lambda r: equipes.get(r[COL_ID], {}).get("nome", "Sem equipe")  # noqa: E731
        nomes = df.apply(chave, axis=1) if len(df) else pd.Series(dtype=str)
        ordem_nomes = sorted(set(nomes), key=lambda x: (x == "Sem equipe", x))
    else:
        chave = lambda r: r[COL_SITUACAO]  # noqa: E731
        nomes = df[COL_SITUACAO]
        ordem_nomes = list(SITUACAO_CORES) + sorted(set(df[COL_SITUACAO].dropna()) - set(SITUACAO_CORES))
    for g in ordem_nomes:
        n = int((nomes == g).sum())
        if n:
            grupos[g] = folium.FeatureGroup(name=f"{g} ({n})", show=True).add_to(m)

    sel_row = None
    for _, row in df.iterrows():
        fid = row[COL_ID]
        if fid == selected_id:
            sel_row = row
        sit = row[COL_SITUACAO]
        embargada = row[COL_EMBARGO] == "Área Embargada"
        eq = equipes.get(fid)
        cor = cor_equipe(eq["numero"] if eq else None) if cor_por_equipe else SITUACAO_CORES.get(sit, COR_PADRAO)
        folium.CircleMarker(
            location=[row[COL_LAT], row[COL_LON]],
            radius=RAIO_PORTE.get(row[COL_PORTE], 6) + (2 if embargada else 0),
            color=COR_EMBARGO_BORDA if embargada else "#FFFFFF",
            weight=3 if embargada else 1,
            fill=True, fill_color=cor, fill_opacity=0.9,
            tooltip=tooltip_text(row, eq),
            popup=folium.Popup(popup_html(row, rotas.get(fid), eq), max_width=340),
        ).add_to(grupos.get(chave(row), m))

    # --- Origem --------------------------------------------------------------------
    folium.Marker(
        [origem.lat, origem.lon],
        tooltip=f"ORIGEM · {origem.nome}",
        popup=folium.Popup(f"<b>Origem das rotas</b><br>{html.escape(origem.nome)}<br>"
                           f"{origem.lat:.5f}, {origem.lon:.5f}<br><small>{html.escape(origem.tipo)}</small>",
                           max_width=260),
        icon=folium.Icon(color="blue", icon="home", prefix="fa"),
    ).add_to(m)

    # --- Seleção: destaque + foco automático -----------------------------------------
    bounds: list[tuple[float, float]] = []
    if sel_row is not None:
        camada_sel = folium.FeatureGroup(name="Fazenda selecionada", show=True)
        rota_sel = rotas.get(selected_id)
        if rota_sel:
            bounds = _draw_route(camada_sel, rota_sel, highlight=True, max_points=None)
        folium.Marker(
            [sel_row[COL_LAT], sel_row[COL_LON]],
            tooltip=tooltip_text(sel_row, equipes.get(selected_id)),
            popup=folium.Popup(popup_html(sel_row, rota_sel, equipes.get(selected_id)), max_width=340, show=False),
            icon=folium.Icon(color="orange", icon="star", prefix="fa"),
        ).add_to(camada_sel)
        camada_sel.add_to(m)
        if not bounds:
            lat, lon = float(sel_row[COL_LAT]), float(sel_row[COL_LON])
            d = 0.06
            bounds = [(lat - d, lon - d), (lat + d, lon + d)]
    else:
        bounds = [(float(a), float(b)) for a, b in zip(df[COL_LAT], df[COL_LON])] + [(origem.lat, origem.lon)]

    if bounds:
        lats, lons = [p[0] for p in bounds], [p[1] for p in bounds]
        m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(30, 30))

    Fullscreen(position="topleft").add_to(m)
    MeasureControl(position="topleft", primary_length_unit="kilometers").add_to(m)
    legenda_eq, n_sem = None, 0
    if cor_por_equipe:
        legenda_eq = {v["numero"]: f"{v['nome']} · {v['municipio']}" for v in equipes.values()
                      if v["nome"] in set(nomes)}
        n_sem = int((nomes == "Sem equipe").sum())
    _legend(legenda_eq, n_sem).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m
