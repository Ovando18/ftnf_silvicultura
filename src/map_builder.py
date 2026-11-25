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


def tooltip_text(row: pd.Series) -> str:
    """Texto do tooltip: começa SEMPRE pelo ID, usado para identificar o clique."""
    return f"{row[COL_ID]}{TOOLTIP_SEP}{row[COL_NOME]}{TOOLTIP_SEP}{row[COL_MUN]}"


def id_from_tooltip(tooltip: Optional[str]) -> Optional[str]:
    if not tooltip:
        return None
    return str(tooltip).split(TOOLTIP_SEP)[0].strip() or None


def _fmt_num(v, dec: int = 1, suf: str = "") -> str:
    if v is None or pd.isna(v):
        return "—"
    s = f"{float(v):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s}{suf}"


def popup_html(row: pd.Series, rota: Optional[dict]) -> str:
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
    {rota_html}
  </table>
  <div style="margin-top:6px;color:#1565C0"><i>Ficha completa no painel ao lado →</i></div>
</div>"""


def _legend() -> MacroElement:
    itens = "".join(
        f"<div><span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:{c};margin-right:6px'></span>{s}</div>"
        for s, c in SITUACAO_CORES.items()
    )
    tpl = f"""
{{% macro html(this, kwargs) %}}
<div style="position:fixed;bottom:28px;right:10px;z-index:9999;background:rgba(255,255,255,.93);
            padding:8px 10px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);font:12px sans-serif;color:#222">
  <b>Situação IBAMA Consolidada</b>{itens}
  <div style="margin-top:4px"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;
       border:3px solid #000;margin-right:6px"></span>Área Embargada (borda preta)</div>
  <div><span style="display:inline-block;width:18px;border-top:3px solid {COR_ROTA};margin-right:6px"></span>Rota OSRM</div>
  <div><span style="display:inline-block;width:18px;border-top:3px dashed {COR_ROTA};margin-right:6px"></span>Rota estimada (fallback) / acesso</div>
  <div><span style="display:inline-block;width:18px;border-top:4px solid {COR_ROTA_DESTAQUE};margin-right:6px"></span>Rota da fazenda selecionada</div>
  <div style="margin-top:2px">Tamanho do círculo = porte</div>
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
) -> folium.Map:
    """
    df             : fazendas a plotar (já filtradas; somente coordenadas válidas)
    rotas          : {ID: entry de rota} para a origem atual (usado em popups)
    rotas_visiveis : IDs cujas rotas devem ser desenhadas
    selected_id    : fazenda em foco (marcador + rota destacados, zoom automático)
    """
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

    # --- Fazendas por situação -----------------------------------------------------
    grupos: dict[str, folium.FeatureGroup] = {}
    for sit in list(SITUACAO_CORES) + sorted(set(df[COL_SITUACAO].dropna()) - set(SITUACAO_CORES)):
        n = int((df[COL_SITUACAO] == sit).sum())
        if n:
            grupos[sit] = folium.FeatureGroup(name=f"{sit} ({n})", show=True).add_to(m)

    sel_row = None
    for _, row in df.iterrows():
        fid = row[COL_ID]
        if fid == selected_id:
            sel_row = row
        sit = row[COL_SITUACAO]
        embargada = row[COL_EMBARGO] == "Área Embargada"
        cor = SITUACAO_CORES.get(sit, COR_PADRAO)
        folium.CircleMarker(
            location=[row[COL_LAT], row[COL_LON]],
            radius=RAIO_PORTE.get(row[COL_PORTE], 6) + (2 if embargada else 0),
            color=COR_EMBARGO_BORDA if embargada else "#FFFFFF",
            weight=3 if embargada else 1,
            fill=True, fill_color=cor, fill_opacity=0.9,
            tooltip=tooltip_text(row),
            popup=folium.Popup(popup_html(row, rotas.get(fid)), max_width=340),
        ).add_to(grupos.get(sit, m))

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
            tooltip=tooltip_text(sel_row),
            popup=folium.Popup(popup_html(sel_row, rota_sel), max_width=340, show=False),
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
    _legend().add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m
