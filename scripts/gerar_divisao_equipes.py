"""Gera os arquivos da divisão em 18 equipes em data/equipes/.

Uso:  python scripts/gerar_divisao_equipes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.data_loader import load_dataset  # noqa: E402
from src.routing import ORIGEM_PADRAO  # noqa: E402
from src.teams import dividir_por_municipio, tabela_equipes  # noqa: E402

OUT = ROOT / "data" / "equipes"


def main() -> None:
    ds = load_dataset()
    df = ds.cadastros[ds.cadastros["coord_valida"]]
    plano = dividir_por_municipio(df, ORIGEM_PADRAO)
    tab = tabela_equipes(plano, df)
    cols_extra = ["Município", "Produtor / Detentor", "Porte", "Área Efetiva Plantada (ha)",
                  "Situação IBAMA Consolidada", "Embargo IBAMA"]
    tab = tab.merge(df[["ID SilviRadar"] + cols_extra], on="ID SilviRadar", how="left")
    OUT.mkdir(parents=True, exist_ok=True)
    tab.to_csv(OUT / "divisao_18_equipes.csv", index=False, sep=";", encoding="utf-8-sig")
    resumo = pd.DataFrame([{
        "Nº": e.numero, "Equipe": e.nome, "Município": a.municipio, "Propriedades": len(a.ordem_visita),
        "Origem → 1ª visita (km, reta)": a.origem_ate_primeira_km,
        "Percurso entre fazendas (km, reta)": a.percurso_km,
        "Sequência de visita": " → ".join(a.ordem_visita),
    } for e, a in zip(plano.equipes, plano.atribuicoes)])
    resumo.to_csv(OUT / "resumo_equipes.csv", index=False, sep=";", encoding="utf-8-sig")
    (OUT / "plano_18_equipes.json").write_text(plano.to_json(), encoding="utf-8")
    print(resumo.drop(columns="Sequência de visita").to_string(index=False))
    sem = df[~df["ID SilviRadar"].isin(tab["ID SilviRadar"])]
    if len(sem):
        print(f"\nSem equipe: {len(sem)} propriedades ({', '.join(sorted(sem['Município'].unique()))})")


if __name__ == "__main__":
    main()
