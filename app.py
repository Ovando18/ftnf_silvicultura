"""Força Tarefa Nacional de Fiscalização — Silvicultura (Eucalipto) / MS.

Mapa interativo das propriedades, ficha completa ao clicar e rotas rodoviárias ponto a ponto
saindo de Três Lagoas - MS até cada fazenda.

Execute:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import io
import json
import time

import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from src.data_loader import (
    COL_EMBARGO, COL_ID, COL_LAT, COL_LON, COL_MUN, COL_NOME, COL_PORTE, COL_SITUACAO,
    DEFAULT_XLSX, Dataset, field_groups_for, load_dataset,
)
from src.map_builder import SITUACAO_CORES, TILES, build_map, cor_equipe, id_from_tooltip
from src.routing import (
    METODO_FALLBACK, METODO_OSRM, ORIGEM_PADRAO, TRES_LAGOAS_CENTRO, Origem, RouteCache, RouteParams,
    compute_routes, format_duration, geocode, haversine_km, route_coords,
)
from src.teams import SEM_EQUIPE, PlanoDivisao, calcular_prioridade, dividir_por_municipio, tabela_equipes

st.set_page_config(page_title="Força Tarefa · Silvicultura MS", page_icon="🌲", layout="wide")

st.markdown(
    """<style>
    .block-container{padding-top:1.2rem;padding-bottom:1rem}
    div[data-testid="stMetricValue"]{font-size:1.35rem}
    .badge{display:inline-block;padding:2px 8px;border-radius:10px;color:#fff;font-size:.8rem;font-weight:600;margin-right:4px}
    .ficha td{padding:2px 6px;vertical-align:top;font-size:.86rem}
    .ficha td:first-child{color:#666;white-space:nowrap}
    </style>""",
    unsafe_allow_html=True,
)


# =============================================================================
# Utilidades
# =============================================================================

def fmt_br(v, dec: int = 0) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


@st.cache_data(show_spinner="Carregando planilha…")
def carregar(conteudo: bytes | None) -> Dataset:
    return load_dataset(io.BytesIO(conteudo) if conteudo else DEFAULT_XLSX)


@st.cache_resource
def route_cache() -> RouteCache:
    return RouteCache()


@st.cache_data(ttl=24 * 3600, show_spinner="Geocodificando Três Lagoas - MS…")
def geocode_tres_lagoas() -> tuple[float, float] | None:
    return geocode("Três Lagoas, Mato Grosso do Sul, Brasil")


@st.cache_data(show_spinner="Dividindo propriedades entre as equipes…")
def plano_equipes(df: pd.DataFrame) -> tuple[PlanoDivisao, pd.DataFrame]:
    """Divisão oficial em 18 equipes (fixa, referenciada ao centro de Três Lagoas - MS)."""
    plano = dividir_por_municipio(df, ORIGEM_PADRAO)
    return plano, tabela_equipes(plano, df)


def selecionar(fid: str | None) -> None:
    """Agenda a seleção de uma fazenda (aplicada antes de desenhar o seletor)."""
    st.session_state["_pendente"] = fid


# =============================================================================
# Dados
# =============================================================================

with st.sidebar:
    st.header("🌲 Força Tarefa · Silvicultura MS")
    upload = st.file_uploader("Planilha (opcional — padrão: base do repositório)", type=["xlsx"])

try:
    ds = carregar(upload.getvalue() if upload else None)
except Exception as exc:  # noqa: BLE001
    st.error(f"Não foi possível carregar a planilha: {exc}")
    st.stop()

base = ds.cadastros
validos = base[base["coord_valida"]].copy()
todos_ids = base[COL_ID].tolist()
por_id = base.set_index(COL_ID, drop=False)
cache = route_cache()

try:
    plano, tab_eq = plano_equipes(validos)
except ValueError as exc:  # planilha diferente da prevista na divisão
    st.sidebar.error(f"Divisão de equipes não aplicada: {exc}")
    plano, tab_eq = None, pd.DataFrame(columns=[COL_ID, "Equipe", "Nº Equipe", "Ordem de Visita",
                                                "Total da Equipe", "Município da Equipe"])
equipe_de = {
    r[COL_ID]: {"numero": int(r["Nº Equipe"]), "nome": r["Equipe"], "ordem": int(r["Ordem de Visita"]),
                "total": int(r["Total da Equipe"]), "municipio": r["Município da Equipe"]}
    for _, r in tab_eq.iterrows()
}
nomes_equipes = sorted(tab_eq["Equipe"].unique().tolist())

# =============================================================================
# Sidebar — origem, filtros e parâmetros
# =============================================================================

with st.sidebar:
    st.subheader("📍 Origem das rotas")
    tipo_origem = st.radio(
        "Ponto de partida",
        ["Três Lagoas - MS · centro (coordenada padrão)",
         "Três Lagoas - MS · centro (geocoding)",
         "Fazenda base em Três Lagoas",
         "Coordenada personalizada"],
        index=0,
        help="Padrão: centro urbano de Três Lagoas - MS. As rotas ficam em cache separadas por origem.",
    )
    origem = ORIGEM_PADRAO
    if tipo_origem.endswith("(geocoding)"):
        coord = geocode_tres_lagoas()
        if coord:
            origem = Origem("Três Lagoas - MS (geocoding)", coord[0], coord[1], "Centro urbano via Nominatim/OSM")
        else:
            st.warning("Geocoding indisponível — usando a coordenada padrão de Três Lagoas.")
    elif tipo_origem.startswith("Fazenda base"):
        tl = validos[validos[COL_MUN] == "Três Lagoas"].sort_values(COL_NOME)
        fb = st.selectbox("Fazenda base", tl[COL_ID].tolist(),
                          format_func=lambda i: f"{i} · {por_id.at[i, COL_NOME]}")
        if fb:
            r = por_id.loc[fb]
            origem = Origem(f"Base: {r[COL_NOME]} ({fb}) — Três Lagoas", float(r[COL_LAT]), float(r[COL_LON]),
                            "Fazenda base em Três Lagoas - MS")
    elif tipo_origem.startswith("Coordenada"):
        c1, c2 = st.columns(2)
        la = c1.number_input("Latitude", value=TRES_LAGOAS_CENTRO[0], format="%.5f")
        lo = c2.number_input("Longitude", value=TRES_LAGOAS_CENTRO[1], format="%.5f")
        origem = Origem(f"Coordenada ({la:.4f}, {lo:.4f})", float(la), float(lo), "Personalizada")
    st.caption(f"`{origem.lat:.5f}, {origem.lon:.5f}`")

    with st.expander("⚙️ Parâmetros de rota", expanded=False):
        p_delay = st.slider("Pausa entre requisições OSRM (s)", 0.0, 2.0, 0.4, 0.1,
                            help="Respeita o limite do servidor público do OSRM em lotes.")
        p_timeout = st.slider("Timeout por requisição (s)", 3, 30, 12)
        p_fator = st.slider("Fator de sinuosidade (fallback)", 1.0, 2.0, 1.35, 0.05,
                            help="Distância rodoviária estimada = distância geodésica × fator.")
        p_vel = st.slider("Velocidade média (fallback, km/h)", 30, 100, 60, 5)
        p_url = st.text_input("Servidor OSRM", RouteParams().osrm_url)
        auto_sel = st.checkbox("Calcular automaticamente a rota da fazenda selecionada", value=True)
    params = RouteParams(osrm_url=p_url, timeout_s=p_timeout, delay_s=p_delay,
                         fator_sinuosidade=p_fator, velocidade_fallback_kmh=p_vel)

    rotas = cache.for_origin(origem)

    st.subheader("🔎 Filtros")
    busca = st.text_input("Busca (ID, fazenda, produtor, CPF/CNPJ, CAR)")
    f_eq = st.multiselect("Equipe", nomes_equipes + [SEM_EQUIPE], placeholder="Todas")

    def ms(label, col, default=None):
        opts = sorted(base[col].dropna().unique().tolist())
        return st.multiselect(label, opts, default=default or [], placeholder="Todos")

    f_mun = ms("Município", COL_MUN)
    f_sit = ms("Situação IBAMA Consolidada", COL_SITUACAO)
    f_emb = ms("Embargo IBAMA", COL_EMBARGO)
    f_porte = ms("Porte", COL_PORTE)
    with st.expander("Mais filtros"):
        f_fin = ms("Finalidade Principal", "Finalidade Principal")
        f_cert = ms("Certificação Florestal", "Certificação Florestal")
        f_esp = ms("Espécie Cultivada", "Espécie Cultivada")
        f_cr = ms("Certificado de Regularidade (CR)", "Certificado de Regularidade (CR)")
        f_ctf = ms("Situação Cadastral CTF", "Situação Cadastral CTF")
        f_car = ms("Situação CAR", "Situação CAR")
        f_lic = ms("Licenciamento Ambiental Estadual", "Licenciamento Ambiental Estadual")
        f_dof = ms("Vínculo DOF / Sinaflor", "Vínculo DOF / Sinaflor")
        amax = float(base["Área Efetiva Plantada (ha)"].max() or 0)
        f_area = st.slider("Área Efetiva Plantada (ha)", 0.0, amax, (0.0, amax), step=10.0)
        f_dist = st.slider("Distância da origem (km)", 0, 800, (0, 800), step=10,
                           help="Usa a distância rodoviária calculada; sem rota, a estimativa geodésica × fator.")
        f_rota = st.selectbox("Status da rota", ["Todas", "Com rota OSRM", "Com rota estimada (fallback)",
                                                 "Sem rota calculada"])

    st.subheader("🗺️ Mapa")
    tiles = st.selectbox("Mapa base", list(TILES))
    cor_por = st.radio("Colorir fazendas por", ["Situação IBAMA", "Equipe"], horizontal=True)
    seq_modo = st.selectbox("Sequência de visita das equipes",
                            ["Equipes filtradas", "Somente a equipe da fazenda selecionada", "Não mostrar"],
                            help="Linha ligando as propriedades na ordem de visita de cada equipe.")
    seq_rotulos = st.checkbox("Numerar a ordem de visita no mapa", value=True,
                              help="Rótulos aparecem quando até 3 equipes estão na tela.")

# =============================================================================
# Aplicação dos filtros
# =============================================================================

df = validos.copy()
df["Prioridade"] = df.apply(calcular_prioridade, axis=1)
df["_dist_ref"] = [
    rotas[i]["distancia_km"] if i in rotas else haversine_km(origem.lat, origem.lon, la_, lo_) * p_fator
    for i, la_, lo_ in zip(df[COL_ID], df[COL_LAT], df[COL_LON])
]
df["_metodo"] = df[COL_ID].map(lambda i: rotas[i]["metodo"] if i in rotas else None)
df["Equipe"] = df[COL_ID].map(lambda i: equipe_de[i]["nome"] if i in equipe_de else SEM_EQUIPE)
df["Ordem de Visita"] = df[COL_ID].map(lambda i: equipe_de[i]["ordem"] if i in equipe_de else None).astype("Int64")

if busca:
    q = busca.strip().lower()
    cols_busca = [COL_ID, COL_NOME, "Produtor / Detentor", "CPF/CNPJ (mascarado)", "Código CAR"]
    mask = pd.Series(False, index=df.index)
    for c in cols_busca:
        mask |= df[c].astype(str).str.lower().str.contains(q, regex=False, na=False)
    df = df[mask]
for col, sel in [("Equipe", f_eq), (COL_MUN, f_mun), (COL_SITUACAO, f_sit), (COL_EMBARGO, f_emb), (COL_PORTE, f_porte),
                 ("Finalidade Principal", f_fin), ("Certificação Florestal", f_cert), ("Espécie Cultivada", f_esp),
                 ("Certificado de Regularidade (CR)", f_cr), ("Situação Cadastral CTF", f_ctf),
                 ("Situação CAR", f_car), ("Licenciamento Ambiental Estadual", f_lic),
                 ("Vínculo DOF / Sinaflor", f_dof)]:
    if sel:
        df = df[df[col].isin(sel)]
df = df[df["Área Efetiva Plantada (ha)"].fillna(0).between(*f_area)]
if f_dist != (0, 800):
    df = df[df["_dist_ref"].between(f_dist[0], f_dist[1] if f_dist[1] < 800 else 1e9)]
if f_rota == "Com rota OSRM":
    df = df[df["_metodo"] == METODO_OSRM]
elif f_rota == "Com rota estimada (fallback)":
    df = df[df["_metodo"] == METODO_FALLBACK]
elif f_rota == "Sem rota calculada":
    df = df[df["_metodo"].isna()]

filtrados_ids = df[COL_ID].tolist()

# =============================================================================
# Cabeçalho, aviso e KPIs
# =============================================================================

st.title("Força Tarefa Nacional de Fiscalização — Silvicultura (Eucalipto) · MS")
st.warning(f"**Aviso (aba Metadados):** {ds.aviso}", icon="⚠️")

msgs = ds.report.mensagens()
with st.expander(f"Validação da base — {ds.report.validos}/{ds.report.total} registros georreferenciados válidos"
                 + ("" if ds.report.ok else " · ⚠ verificar"), expanded=not ds.report.ok):
    st.markdown(f"- `ID SilviRadar` únicos: **{'sim' if not ds.report.ids_duplicados else 'não'}**  \n"
                f"- Coordenadas inválidas: **{len(ds.report.coords_invalidas)}** · fora de MS: "
                f"**{len(ds.report.coords_fora_ms)}**")
    for nivel, msg in msgs:
        getattr(st, nivel)(msg)
    if ds.metadados:
        st.dataframe(pd.DataFrame(list(ds.metadados.items()), columns=["Metadado", "Valor"]),
                     hide_index=True, width="stretch")

rotas_filtradas = [rotas[i] for i in filtrados_ids if i in rotas]
k = st.columns(7)
k[0].metric("Propriedades", f"{len(df)}", f"de {len(base)}", delta_color="off", delta_arrow="off")
k[1].metric("Área plantada (ha)", fmt_br(df["Área Efetiva Plantada (ha)"].sum()))
k[2].metric("Estoque estimado (m³)", fmt_br(df["Estoque Estimado (m³)"].sum()))
k[3].metric("Áreas embargadas", int((df[COL_EMBARGO] == "Área Embargada").sum()))
k[4].metric("Pendência / alerta", int((df[COL_SITUACAO] != "Regular").sum()))
n_osrm = sum(r["metodo"] == METODO_OSRM for r in rotas_filtradas)
k[5].metric("Rotas calculadas", f"{len(rotas_filtradas)}/{len(df)}",
            f"{n_osrm} OSRM · {len(rotas_filtradas) - n_osrm} estim.", delta_color="off", delta_arrow="off")
k[6].metric("Distância média (km)",
            fmt_br(sum(r["distancia_km"] for r in rotas_filtradas) / len(rotas_filtradas), 1) if rotas_filtradas else "—")

# =============================================================================
# Seleção da fazenda
# =============================================================================

if "_pendente" in st.session_state:
    st.session_state["sel"] = st.session_state.pop("_pendente")
opcoes = [None] + todos_ids
sel_c1, sel_c2 = st.columns([4, 1])
selected_id = sel_c1.selectbox(
    "🎯 Fazenda selecionada (clique no mapa, na tabela ou busque aqui)",
    opcoes, key="sel",
    format_func=lambda i: "— nenhuma —" if i is None else
    f"{i} · {por_id.at[i, COL_NOME]} · {por_id.at[i, COL_MUN]} · {por_id.at[i, COL_SITUACAO]}",
)
sel_c2.write("")
sel_c2.button("Limpar seleção", on_click=selecionar, args=(None,), disabled=selected_id is None, width="stretch")

if selected_id and auto_sel and selected_id not in rotas and bool(por_id.at[selected_id, "coord_valida"]):
    r = por_id.loc[selected_id]
    with st.spinner("Calculando rota da fazenda selecionada…"):
        compute_routes([(selected_id, float(r[COL_LAT]), float(r[COL_LON]))], origem, cache,
                       RouteParams(**{**params.__dict__, "delay_s": 0}))
    rotas = cache.for_origin(origem)

# =============================================================================
# Controles de rotas
# =============================================================================

with st.container(border=True):
    st.markdown("##### 🛣️ Rotas ponto a ponto — origem: " + origem.nome)
    c1, c2, c3 = st.columns([1.3, 1.4, 1.6])
    with c1:
        escopo = st.radio("Calcular / atualizar para",
                          ["Fazenda selecionada", f"Registros filtrados ({len(df)})",
                           f"Todas as {len(validos)} fazendas"], index=1)
        so_cache = st.checkbox("Usar apenas o cache já salvo", value=False)
        refazer_fb = st.checkbox("Tentar OSRM novamente nas rotas estimadas", value=True)
        forcar = st.checkbox("Forçar recálculo (ignorar cache)", value=False)
        calcular = st.button("▶ Calcular / Atualizar rotas", type="primary", width="stretch")
    with c2:
        exibir = st.radio("Exibir rotas no mapa",
                          ["Somente a fazenda selecionada", "Fazendas filtradas", "Todas as calculadas", "Nenhuma"],
                          index=1)
        limite = st.slider("Limite de rotas desenhadas", 10, 411, 50, 10,
                           help="Evita travar o navegador. Ordem: mais próximas da origem primeiro.")
        pts_rota = st.select_slider("Pontos por rota (rotas não destacadas)", [100, 200, 400, 800, 2000], 400)
    with c3:
        placeholder_prog = st.empty()
        n_cache_total = len(rotas)
        n_osrm_total = sum(r["metodo"] == METODO_OSRM for r in rotas.values())
        st.caption(f"Cache para esta origem: **{n_cache_total}** rotas ({n_osrm_total} OSRM, "
                   f"{n_cache_total - n_osrm_total} estimadas) · arquivo `{cache.path.relative_to(cache.path.parents[2])}`")
        if st.button("🗑 Limpar cache desta origem"):
            cache.clear_origin(origem)
            cache.save()
            st.rerun()

    if calcular:
        if escopo == "Fazenda selecionada":
            alvo = [selected_id] if selected_id else []
        elif escopo.startswith("Registros filtrados"):
            alvo = filtrados_ids
        else:
            alvo = validos[COL_ID].tolist()
        if not alvo:
            placeholder_prog.warning("Nenhuma fazenda no escopo escolhido.")
        else:
            farms = [(i, float(por_id.at[i, COL_LAT]), float(por_id.at[i, COL_LON])) for i in alvo]
            bar = placeholder_prog.progress(0.0, text="Iniciando…")
            t0 = time.time()

            def prog(i, n, fid):
                el = time.time() - t0
                eta = el / i * (n - i)
                bar.progress(i / n, text=f"{i}/{n} · {fid} · restante ≈ {format_duration(eta)}")

            summ = compute_routes(farms, origem, cache, params, use_cache_only=so_cache, force=forcar,
                                  retry_fallback=refazer_fb, progress=prog)
            st.session_state["_ultimo_lote"] = summ.as_dict()
            st.rerun()
    if "_ultimo_lote" in st.session_state:
        s = st.session_state["_ultimo_lote"]
        placeholder_prog.success(
            f"Último lote: {s['solicitadas']} solicitadas · {s['do_cache']} do cache · {s['osrm']} via OSRM · "
            f"{s['fallback']} estimadas (fallback)" + (f" · {s['sem_cache']} sem cache" if s["sem_cache"] else ""))
        if s["fallback"] and not s["osrm"]:
            st.info("Todas as novas rotas caíram no fallback: o servidor OSRM pode estar inacessível a partir "
                    "deste ambiente (rede/firewall) ou fora do ar. As distâncias exibidas são estimadas.")

# Rotas a desenhar
if exibir == "Somente a fazenda selecionada":
    candidatos = []
elif exibir == "Fazendas filtradas":
    candidatos = [i for i in filtrados_ids if i in rotas]
elif exibir == "Todas as calculadas":
    candidatos = list(rotas)
else:
    candidatos = []
candidatos.sort(key=lambda i: rotas[i]["distancia_km"])
rotas_visiveis = candidatos[:limite]
if len(candidatos) > limite:
    st.caption(f"Exibindo {limite} de {len(candidatos)} rotas (as mais próximas). Ajuste o limite para ver mais.")

# =============================================================================
# Mapa + ficha
# =============================================================================

col_mapa, col_ficha = st.columns([1.75, 1])

with col_mapa:
    plot = df
    if selected_id and selected_id not in filtrados_ids and bool(por_id.at[selected_id, "coord_valida"]):
        plot = pd.concat([df, validos[validos[COL_ID] == selected_id]])
    # Sequências de visita a desenhar
    if seq_modo == "Equipes filtradas":
        eqs_seq = sorted({equipe_de[i]["nome"] for i in filtrados_ids if i in equipe_de})
    elif seq_modo.startswith("Somente") and selected_id in equipe_de:
        eqs_seq = [equipe_de[selected_id]["nome"]]
    else:
        eqs_seq = []
    sequencias = []
    for nome in eqs_seq:
        t_ = tab_eq[tab_eq["Equipe"] == nome].sort_values("Ordem de Visita")
        sequencias.append({"numero": int(t_["Nº Equipe"].iloc[0]), "nome": nome,
                           "coords": list(zip(t_[COL_LAT], t_[COL_LON])),
                           "rotulos": seq_rotulos and len(eqs_seq) <= 3})
    mapa = build_map(plot, origem, rotas, rotas_visiveis, selected_id, tiles=tiles, max_points_por_rota=pts_rota,
                     equipes=equipe_de, cor_por_equipe=(cor_por == "Equipe"), sequencias=sequencias)
    assinatura = hashlib.md5(json.dumps([selected_id, origem.key, filtrados_ids, rotas_visiveis, tiles,
                                         pts_rota, len(rotas), cor_por, eqs_seq, seq_rotulos]).encode()).hexdigest()[:12]
    map_key = f"mapa_{assinatura}"
    out = st_folium(mapa, key=map_key, height=680, use_container_width=True,
                    returned_objects=["last_object_clicked_tooltip"])
    tip = (out or {}).get("last_object_clicked_tooltip")
    clicado = id_from_tooltip(tip)
    if clicado and clicado in por_id.index and (map_key, tip) != st.session_state.get("_clique_tratado"):
        st.session_state["_clique_tratado"] = (map_key, tip)
        if clicado != selected_id:
            selecionar(clicado)
            st.rerun()
    if selected_id and selected_id not in filtrados_ids:
        st.caption("A fazenda selecionada não atende aos filtros atuais, mas é exibida para referência.")

with col_ficha:
    if not selected_id:
        st.info("Clique em uma fazenda no mapa, selecione uma linha na tabela ou use a busca para abrir a "
                "**ficha completa da propriedade**.")
        st.markdown("**Legenda de situação**")
        for s_, c_ in SITUACAO_CORES.items():
            n_ = int((df[COL_SITUACAO] == s_).sum())
            st.markdown(f"<span class='badge' style='background:{c_}'>{s_}</span> {n_}", unsafe_allow_html=True)
    else:
        r = por_id.loc[selected_id]
        cor = SITUACAO_CORES.get(r[COL_SITUACAO], "#607D8B")
        emb = r[COL_EMBARGO] == "Área Embargada"
        st.markdown(f"### {r[COL_NOME]}")
        st.markdown(
            f"<span class='badge' style='background:#455A64'>{selected_id}</span>"
            f"<span class='badge' style='background:{cor}'>{r[COL_SITUACAO]}</span>"
            f"<span class='badge' style='background:{'#000' if emb else '#78909C'}'>"
            f"{'⚠ ' if emb else ''}{r[COL_EMBARGO]}</span>"
            f"<span class='badge' style='background:#6D4C41'>Prioridade {calcular_prioridade(r)}</span>"
            + (f"<span class='badge' style='background:{cor_equipe(equipe_de[selected_id]['numero'])}'>"
               f"{equipe_de[selected_id]['nome']} · visita {equipe_de[selected_id]['ordem']}/"
               f"{equipe_de[selected_id]['total']}</span>" if selected_id in equipe_de else
               f"<span class='badge' style='background:#9E9E9E'>{SEM_EQUIPE}</span>"),
            unsafe_allow_html=True)
        st.caption(f"{r['Produtor / Detentor']} · {r[COL_MUN]}/MS")

        rota = rotas.get(selected_id)
        with st.container(border=True):
            st.markdown(f"**Rota a partir de {origem.nome}**")
            if rota:
                m1, m2, m3 = st.columns(3)
                m1.metric("Distância", f"{fmt_br(rota['distancia_km'], 1)} km")
                m2.metric("Tempo estimado", format_duration(rota["duracao_s"]))
                m3.metric("Linha reta", f"{fmt_br(rota['geodesica_km'], 1)} km")
                icone = "✅" if rota["metodo"] == METODO_OSRM else "⚠️"
                st.caption(f"{icone} Método: **{rota['metodo']}** · {rota.get('status', '')} · "
                           f"calculado em {rota.get('calculado_em', '')[:16].replace('T', ' ')}")
                gmaps = (f"https://www.google.com/maps/dir/?api=1&origin={origem.lat},{origem.lon}"
                         f"&destination={r[COL_LAT]},{r[COL_LON]}&travelmode=driving")
                st.link_button("Abrir navegação (Google Maps)", gmaps, width="stretch")
            else:
                st.caption("Rota ainda não calculada para esta origem. Use **Calcular / Atualizar rotas** "
                           "com escopo *Fazenda selecionada*.")

        def valor(c):
            v = r.get(c)
            if v is None or pd.isna(v):
                return "—"
            if c in (COL_LAT, COL_LON):
                return f"{float(v):.6f}"
            if c in ("Código IBGE Município", "Registro CTF/APP IBAMA", "Ano de Implantação"):
                return str(v)
            if isinstance(v, float):
                return fmt_br(v, 1)
            if isinstance(v, int) or (hasattr(v, "dtype") and "int" in str(getattr(v, "dtype", ""))):
                return fmt_br(v, 0)
            try:
                if float(v) == int(v) and not isinstance(v, str):
                    return fmt_br(v, 0)
            except (TypeError, ValueError):
                pass
            return str(v)

        for i, (grupo, cols) in enumerate(field_groups_for(base).items()):
            with st.expander(grupo, expanded=i < 3 or (grupo == "Regularidade IBAMA")):
                linhas = "".join(f"<tr><td>{c}</td><td><b>{valor(c)}</b></td></tr>" for c in cols)
                st.markdown(f"<table class='ficha'>{linhas}</table>", unsafe_allow_html=True)

# =============================================================================
# Tabela ponto a ponto + exportação
# =============================================================================

st.subheader("📋 Resumo ponto a ponto")
tab = df[[COL_ID, COL_NOME, COL_MUN, COL_PORTE, COL_SITUACAO, COL_EMBARGO, "Prioridade", "Equipe",
          "Ordem de Visita", COL_LAT, COL_LON]].copy()
tab["Distância (km)"] = tab[COL_ID].map(lambda i: round(rotas[i]["distancia_km"], 1) if i in rotas else None)
tab["Tempo Estimado"] = tab[COL_ID].map(lambda i: format_duration(rotas[i]["duracao_s"]) if i in rotas else "—")
tab["Método da Rota"] = tab[COL_ID].map(lambda i: rotas[i]["metodo"] if i in rotas else "Não calculada")
ordenar = st.radio("Ordenar tabela por", ["Equipe e ordem de visita", "Distância da origem"], horizontal=True)
if ordenar.startswith("Equipe"):
    tab = tab.sort_values(["Equipe", "Ordem de Visita", COL_ID], na_position="last").reset_index(drop=True)
else:
    tab = tab.sort_values(["Distância (km)", COL_ID], na_position="last").reset_index(drop=True)

ordem_cols = [COL_ID, COL_NOME, COL_MUN, "Equipe", "Ordem de Visita", COL_PORTE, COL_SITUACAO, "Distância (km)",
              "Tempo Estimado", "Método da Rota", COL_EMBARGO, "Prioridade"]
evento = st.dataframe(
    tab[ordem_cols], hide_index=True, width="stretch", height=360,
    on_select="rerun", selection_mode="single-row", key="tabela",
    column_config={
        "Distância (km)": st.column_config.NumberColumn(format="%.1f"),
        "Prioridade": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
    },
)
linhas_sel = evento.selection.rows if evento and evento.selection else []
if linhas_sel:
    fid_tab = tab.iloc[linhas_sel[0]][COL_ID]
    if fid_tab != st.session_state.get("_tabela_tratada"):
        st.session_state["_tabela_tratada"] = fid_tab
        if fid_tab != selected_id:
            selecionar(fid_tab)
            st.rerun()

# Exportação
export = df.drop(columns=["_dist_ref", "_metodo", "coord_valida"]).merge(
    tab[[COL_ID, "Distância (km)", "Tempo Estimado", "Método da Rota"]], on=COL_ID, how="left")
export["Origem da Rota"] = origem.nome
export["Duração (min)"] = export[COL_ID].map(lambda i: round(rotas[i]["duracao_s"] / 60) if i in rotas else None)


def to_geojson(frame: pd.DataFrame, com_rotas: bool) -> str:
    feats = []
    for _, row in frame.iterrows():
        props = {k_: (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)) for k_, v in row.items()}
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [float(row[COL_LON]), float(row[COL_LAT])]}})
        if com_rotas and row[COL_ID] in rotas:
            e = rotas[row[COL_ID]]
            feats.append({"type": "Feature",
                          "properties": {COL_ID: row[COL_ID], "tipo": "rota", "metodo": e["metodo"],
                                         "distancia_km": e["distancia_km"], "duracao_s": e["duracao_s"],
                                         "origem": e["origem_nome"]},
                          "geometry": {"type": "LineString",
                                       "coordinates": [[lo_, la_] for la_, lo_ in route_coords(e)]}})
    return json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, default=str)


e1, e2, e3, e4 = st.columns(4)
e1.download_button("⬇ CSV (filtrados + rotas)", export.to_csv(index=False, sep=";").encode("utf-8-sig"),
                   "fazendas_filtradas_rotas.csv", "text/csv", width="stretch")
e2.download_button("⬇ GeoJSON (pontos)", to_geojson(export, False).encode("utf-8"),
                   "fazendas_filtradas.geojson", "application/geo+json", width="stretch")
e3.download_button("⬇ GeoJSON (pontos + rotas)", to_geojson(export, True).encode("utf-8"),
                   "fazendas_filtradas_rotas.geojson", "application/geo+json", width="stretch")
e4.download_button("⬇ Cache de rotas (JSON)",
                   json.dumps(rotas, ensure_ascii=False).encode("utf-8"),
                   "rotas_tres_lagoas_fazendas.json", "application/json", width="stretch")

# =============================================================================
# Divisão em equipes
# =============================================================================

st.subheader("👥 Divisão em equipes de fiscalização")
if plano is None:
    st.info("A divisão em equipes não pôde ser aplicada a esta planilha (veja o aviso na barra lateral).")
else:
    st.caption(plano.observacoes + " O percurso é a soma das distâncias em linha reta entre propriedades "
               "consecutivas (referência para planejamento; a distância rodoviária será maior).")
    resumo = []
    for eq, a in zip(plano.equipes, plano.atribuicoes):
        sub = base[base[COL_ID].isin(a.ordem_visita)]
        resumo.append({
            "Nº": eq.numero, "Equipe": eq.nome, "Município": a.municipio, "Propriedades": len(a.ordem_visita),
            "Área plantada (ha)": round(float(sub["Área Efetiva Plantada (ha)"].sum()), 1),
            "Embargadas": int((sub[COL_EMBARGO] == "Área Embargada").sum()),
            "Pendência / alerta": int((sub[COL_SITUACAO] != "Regular").sum()),
            "Origem → 1ª visita (km, reta)": a.origem_ate_primeira_km,
            "Percurso entre fazendas (km, reta)": a.percurso_km,
            "1ª visita": a.ordem_visita[0], "Última visita": a.ordem_visita[-1],
        })
    resumo_df = pd.DataFrame(resumo)
    sem = base[~base[COL_ID].isin(equipe_de)]
    st.dataframe(resumo_df, hide_index=True, width="stretch",
                 column_config={"Nº": st.column_config.NumberColumn(width="small")})
    if len(sem):
        st.warning(f"**{len(sem)} propriedades sem equipe** — município(s) não incluído(s) na divisão: "
                   f"{', '.join(sorted(sem[COL_MUN].unique()))}.")

    d1, d2 = st.columns([1, 2])
    eq_det = d1.selectbox("Sequência de visita da equipe", resumo_df["Equipe"].tolist())
    seq_df = tab_eq[tab_eq["Equipe"] == eq_det].merge(
        base[[COL_ID, COL_PORTE, COL_SITUACAO, COL_EMBARGO]], on=COL_ID, how="left")
    d1.metric("Propriedades", len(seq_df))
    d1.metric("Percurso entre fazendas (km, reta)", fmt_br(seq_df["Dist. da anterior (km, linha reta)"].sum(), 1))
    d2.dataframe(seq_df[["Ordem de Visita", COL_ID, "Propriedade / Fazenda", COL_PORTE, COL_SITUACAO,
                         COL_EMBARGO, "Dist. da anterior (km, linha reta)"]],
                 hide_index=True, width="stretch", height=320)

    detalhado = tab_eq.merge(base.drop(columns=["coord_valida", "Propriedade / Fazenda", COL_LAT, COL_LON]),
                             on=COL_ID, how="left")
    q1, q2, q3 = st.columns(3)
    q1.download_button("⬇ Divisão completa (CSV)", detalhado.to_csv(index=False, sep=";").encode("utf-8-sig"),
                       "divisao_18_equipes.csv", "text/csv", width="stretch")
    q2.download_button("⬇ Resumo por equipe (CSV)", resumo_df.to_csv(index=False, sep=";").encode("utf-8-sig"),
                       "resumo_equipes.csv", "text/csv", width="stretch")
    q3.download_button("⬇ Plano (JSON)", plano.to_json().encode("utf-8"), "plano_18_equipes.json",
                       "application/json", width="stretch")

st.caption("Rotas: © OpenStreetMap contributors · OSRM (project-osrm.org). Mapa base: OSM / Esri / CARTO. "
           "Base cadastral sintética de referência — não utilizar para decisões sem consulta às fontes oficiais.")
