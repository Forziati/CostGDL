import anthropic
import streamlit as st

from kernel import SYSTEM_PROMPT

MODELS = {
    "Sonnet 5.5 (mejor calidad)": {
        "id": "claude-sonnet-5-5", "search": "web_search_20260209", "in": 2.0, "out": 10.0,
    },
    "Haiku 4.5 (económico, para probar)": {
        "id": "claude-haiku-4-5-20251001", "search": "web_search_20250305", "in": 1.0, "out": 5.0,
    },
}
SEARCH_USD = 0.01  # aprox. por búsqueda web (USD 10 por 1000)
MAX_SEARCHES = 6
MAX_ROUNDS = 4


def web_search_tool(search_type):
    return {
        "type": search_type,
        "name": "web_search",
        "max_uses": MAX_SEARCHES,
        "user_location": {
            "type": "approximate",
            "city": "Guadalajara",
            "region": "Jalisco",
            "country": "MX",
            "timezone": "America/Mexico_City",
        },
    }


st.set_page_config(page_title="CostGDL", page_icon="🏗️", layout="wide")
st.title("🏗️ CostGDL · Presupuesto de remodelación")

# --- acceso opcional por contraseña -------------------------------------
pwd = st.secrets.get("APP_PASSWORD")
if pwd and st.text_input("Contraseña", type="password") != pwd:
    st.stop()

if "ANTHROPIC_API_KEY" not in st.secrets:
    st.error("Falta ANTHROPIC_API_KEY en .streamlit/secrets.toml (o en Secrets de Streamlit Cloud).")
    st.stop()

client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])

# api_messages: historial para la API (solo texto; los resultados de búsqueda no se reenvían)
# shown: solo texto, para mostrar; versions: cada respuesta del agente
st.session_state.setdefault("api_messages", [])
st.session_state.setdefault("shown", [])
st.session_state.setdefault("model_label", "Haiku 4.5 (económico, para probar)")


def run_agent(live):
    """Ejecuta un turno del agente (con streaming y búsqueda web).

    Dentro del turno se reenvían los bloques de búsqueda para poder reanudar un
    `pause_turn`; al final solo se guarda en el historial el texto de la respuesta,
    para no pagar en cada turno los resultados de búsqueda ya consumidos.
    Devuelve (texto, info) con info = búsquedas, tokens, costo aprox. y diagnóstico.
    """
    cfg = MODELS[st.session_state.model_label]
    turn = list(st.session_state.api_messages)
    streamed, final_text, notice = "", [], ""
    info = {"searches": 0, "in": 0, "out": 0, "rounds": [], "errors": []}
    for _ in range(MAX_ROUNDS):
        with client.messages.stream(
            model=cfg["id"],
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=[web_search_tool(cfg["search"])],
            messages=turn,
        ) as stream:
            for chunk in stream.text_stream:
                streamed += chunk
                live.markdown(streamed + "▌")
            resp = stream.get_final_message()
        turn.append({"role": "assistant", "content": resp.content})
        info["in"] += resp.usage.input_tokens
        info["out"] += resp.usage.output_tokens
        info["rounds"].append(
            f"{resp.stop_reason} · {[b.type for b in resp.content]} · "
            f"in={resp.usage.input_tokens} out={resp.usage.output_tokens}"
        )
        for b in resp.content:
            if b.type == "text":
                final_text.append(b.text)
            else:
                final_text = []
                info["searches"] += b.type == "server_tool_use" and getattr(b, "name", "") == "web_search"
                content = getattr(b, "content", None)
                if b.type == "web_search_tool_result" and not isinstance(content, list):
                    info["errors"].append(str(getattr(content, "error_code", content)))
        if resp.stop_reason == "pause_turn":
            streamed += "\n\n"
            continue
        if resp.stop_reason == "max_tokens":
            notice = "\n\n⚠️ La respuesta se cortó por longitud. Escribe «continúa» para completarla."
        break
    else:
        notice = "\n\n⚠️ Se alcanzó el máximo de rondas de búsqueda. Escribe «continúa» para terminar la propuesta."
    text = "".join(final_text).strip() or streamed.strip()
    st.session_state.api_messages.append({"role": "assistant", "content": text})
    info["usd"] = (
        info["in"] * cfg["in"] + info["out"] * cfg["out"]
    ) / 1e6 + info["searches"] * SEARCH_USD
    return text + notice, info


with st.sidebar:
    st.header("Modelo")
    st.radio("Modelo", list(MODELS), key="model_label", label_visibility="collapsed")
    st.header("Versiones")
    versions = [m["content"] for m in st.session_state.shown if m["role"] == "assistant"]
    if versions:
        st.download_button(
            "⬇️ Descargar última versión (.md)",
            versions[-1],
            file_name=f"presupuesto_v{len(versions)}.md",
        )
        st.caption(f"{len(versions)} versión(es) en esta sesión")
    if st.button("🗑️ Nuevo presupuesto"):
        st.session_state.api_messages = []
        st.session_state.shown = []
        st.rerun()

for m in st.session_state.shown:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

if prompt := st.chat_input("Ej: cocina 4x3 m con isla y baño de 2x2 m, estilo moderno…"):
    st.session_state.shown.append({"role": "user", "content": prompt})
    st.session_state.api_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        live = st.empty()
        live.markdown("Pensando… 🔎")
        try:
            answer, info = run_agent(live)
        except anthropic.APIError as e:
            st.error(f"Error de la API: {e}")
            st.session_state.shown.pop()
            st.session_state.api_messages.pop()
            st.stop()
        live.markdown(answer)
        st.caption(
            f"{info['searches']} búsqueda(s) · {info['in']:,} tokens entrada · "
            f"{info['out']:,} salida · ≈ USD {info['usd']:.2f}"
        )
        with st.expander("Diagnóstico"):
            st.write(info["rounds"])
            if info["errors"]:
                st.warning(f"Errores de búsqueda: {info['errors']}")
    st.session_state.shown.append({"role": "assistant", "content": answer})
    st.rerun()
