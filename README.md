# Força Tarefa Nacional de Fiscalização — Silvicultura (Eucalipto) · MS

Aplicação web em **Streamlit + Folium + streamlit-folium** para apoiar a Força Tarefa no setor de
silvicultura de eucalipto em Mato Grosso do Sul. Usa a planilha `data/silvicultura_força_tarefa_MS.xlsx`
(aba `Cadastros`: 411 registros × 45 colunas; aba `Metadados`).

> ⚠️ **Aviso (aba `Metadados`)** — *Base cadastral de referência SINTÉTICA para demonstração: nomes,
> documentos, códigos CAR e registros CTF são fictícios. Para decisões, consulte as fontes oficiais.*
> Os dados **não** devem embasar autuações ou decisões administrativas sem conferência nas bases oficiais
> (IBAMA CTF/APP, DOF/Sinaflor, Embargos, SICAR, IMASUL).

## Funcionalidades desta etapa

- **Mapa interativo** com as 411 fazendas nos 8 municípios (Três Lagoas, Ribas do Rio Pardo, Água Clara,
  Brasilândia, Selvíria, Inocência, Santa Rita do Pardo, Bataguassu).
  - Cor = `Situação IBAMA Consolidada` (verde *Regular*, amarelo *Com Pendência Cadastral*, vermelho *Com Alerta de Embargo*).
  - Borda preta espessa = `Embargo IBAMA` = *Área Embargada*. Tamanho do círculo = `Porte`.
  - Camadas por situação, mapa base OSM / satélite / claro, tela cheia e régua de medição.
- **Clique na fazenda** (ou seleção pela busca/tabela) → popup resumido + **ficha completa** com os 45 campos
  agrupados (identificação, localização/UTM, dados florestais, DOF/Sinaflor, regularidade IBAMA,
  CAR/APP/RL/licenciamento, fontes e atualização), rota destacada e zoom automático.
- **Rotas rodoviárias ponto a ponto** da origem em **Três Lagoas - MS** até as coordenadas exatas de cada fazenda:
  - Origem: centro urbano (coordenada padrão), centro via geocoding (Nominatim), uma fazenda base em Três Lagoas ou coordenada personalizada.
  - Motor: API pública **OSRM** (`route/v1/driving`, `overview=full`, geometria polyline) → distância (km), tempo (h/min) e traçado.
  - Trecho de **acesso não mapeado**: quando o ponto da fazenda está longe da via mais próxima do OSM, a distância
    de "snap" informada pelo OSRM é somada à rota (a 25 km/h) e desenhada tracejada.
  - **Fallback automático** (timeout, erro HTTP, `NoRoute`/`NoSegment`, rota incoerente): distância geodésica (Haversine)
    × fator de sinuosidade ÷ velocidade média — sinalizado como `Estimado (Fallback)` na coluna *Método da Rota*.
  - Controle de taxa (pausa configurável entre requisições), novas tentativas com backoff em HTTP 429/5xx e
    interrupção automática após 5 falhas seguidas de conexão (evita esperar timeout em todo o lote).
  - **Cache persistente** em `data/cache/rotas_tres_lagoas_fazendas.json`, indexado por `ID SilviRadar` + coordenadas
    da origem (invalida sozinho se a coordenada da fazenda mudar).
  - Botão **Calcular / Atualizar rotas** para *fazenda selecionada*, *registros filtrados* ou *todas as 411*,
    com barra de progresso/ETA, opção "usar apenas o cache", "tentar OSRM novamente nas estimadas" e "forçar recálculo".
  - **Exibição no mapa**: só a selecionada, as filtradas ou todas as calculadas, com limite configurável de
    linhas (padrão 50, as mais próximas primeiro) e simplificação dos pontos por rota.
- **Filtros**: busca textual (ID, fazenda, produtor, CPF/CNPJ, CAR), município, situação IBAMA, embargo, porte,
  finalidade, certificação, espécie, CR, CTF, CAR, licenciamento, DOF/Sinaflor, área plantada, distância da origem
  e status da rota.
- **KPIs**, **tabela ponto a ponto** (`ID SilviRadar`, fazenda, município, porte, situação, distância, tempo, método, embargo, prioridade)
  e **exportação** CSV / GeoJSON (pontos, ou pontos + linhas das rotas) / JSON do cache.
- **Validação** da base: colunas obrigatórias, tipos, nulos, unicidade de `ID SilviRadar`, coordenadas válidas e dentro de MS.

## Estrutura

```
app.py                  # Interface Streamlit: KPIs, filtros, ficha, controles de rota, tabela, exportação
src/data_loader.py      # Leitura/validação de Cadastros e Metadados, tipos/nulos, coordenadas, unicidade do ID
src/routing.py          # OSRM + fallback Haversine, cache JSON persistente, geocoding da origem
src/map_builder.py      # Mapa Folium: marcadores, popups, legenda, camadas de rota, foco na seleção
src/teams.py            # Divisão em equipes: agrupamento por município/proximidade e sequência de visita
scripts/                # gerar_divisao_equipes.py → data/equipes/*.csv|json
data/                   # Planilha de entrada
data/cache/             # Cache das rotas (rotas_tres_lagoas_fazendas.json) e do geocoding
tests/test_core.py      # Testes (pytest) de carga/validação, OSRM simulado, fallback, cache e divisão
```

## Como executar

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Testes:

```bash
pip install -r requirements-dev.txt
pytest -q
```

### Servidor OSRM

O servidor público `router.project-osrm.org` é de demonstração (≈ 1 req/s, sem SLA). Para calcular as 411 rotas
(~3–5 min com pausa de 0,4 s) ele costuma bastar; para uso operacional recomenda-se um OSRM próprio com o extrato
Centro-Oeste do Geofabrik. Configure por variável de ambiente ou no painel *Parâmetros de rota*:

```bash
OSRM_BASE_URL=http://localhost:5000 streamlit run app.py
```

O cache de rotas está no `.gitignore`; para compartilhar rotas já calculadas com a equipe, remova a linha
`data/cache/*.json` do `.gitignore` e versione o arquivo.

## Divisão em 18 equipes de fiscalização

Definida em `src/teams.py` (`DIVISAO_18_EQUIPES`) e aplicada automaticamente no app:

| Equipes | Município | Propriedades por equipe |
|---|---|---|
| 01–05 | Três Lagoas (89) | 18 · 18 · 18 · 18 · 17 |
| 06 | Bataguassu (13) | 13 |
| 07–09 | Brasilândia (60) | 20 · 20 · 20 |
| 10–11 | Inocência (31) | 16 · 15 |
| 12–15 | Ribas do Rio Pardo (81) | 21 · 20 · 20 · 20 |
| 16 | Santa Rita do Pardo (24) | 24 |
| 17–18 | Selvíria (37) | 19 · 18 |

> **Água Clara (76 propriedades) não está incluída na divisão** e aparece como "Sem equipe".

Como a divisão é feita:
- **Agrupamento por localização** nos municípios com mais de uma equipe: k-means com capacidade exata
  (cada grupo recebe exatamente o nº de propriedades definido), refinado por trocas de propriedades entre
  equipes até não haver troca que deixe os grupos mais compactos; 40 inícios diferentes, fica o melhor.
  O resultado é determinístico (semente fixa).
- **Numeração**: começa em Três Lagoas e segue a ordem dos municípios acima; dentro do município, da equipe
  mais próxima para a mais distante do centro de Três Lagoas.
- **Sequência de visita**: começa pela propriedade da equipe mais próxima de Três Lagoas e segue sempre para
  a vizinha mais próxima, com melhoria 2-opt (elimina cruzamentos). Distâncias em linha reta.

No app: filtro **Equipe**, cores **por equipe**, linha e numeração da **sequência de visita** no mapa,
equipe/ordem na ficha e na tabela, resumo por equipe e downloads (CSV completo, resumo, JSON).
Os arquivos também ficam versionados em `data/equipes/` e podem ser regenerados com:

```bash
python scripts/gerar_divisao_equipes.py
```

Para mudar a divisão (ex.: incluir Água Clara), edite `DIVISAO_18_EQUIPES` e rode o script.

---
Rotas © OpenStreetMap contributors · OSRM. Mapa base © OpenStreetMap / Esri / CARTO.
