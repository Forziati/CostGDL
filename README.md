# CostGDL

Agente de presupuesto iterativo para la remodelación de la casa en Guadalajara.
Busca precios vigentes en tiendas mexicanas (Claude + búsqueda web), arma una propuesta
base desglosada por concepto y la recalcula cuando le pides cambios.

## Correr local
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # pega tu API key
streamlit run app.py
```

## Desplegar en Streamlit Cloud
1. share.streamlit.io → New app → repo `Forziati/CostGDL`, archivo `app.py`.
2. Settings → Secrets: `ANTHROPIC_API_KEY = "sk-ant-..."` (y opcional `APP_PASSWORD`).

El prompt del agente está en `kernel.py`; ajústalo ahí.
