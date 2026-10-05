import anthropic
import streamlit as st

from kernel import SYSTEM_PROMPT

MODEL = "claude-sonnet-5-5"
WEB_SEARCH = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 10,
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

# api_messages: historial completo para la API (incluye bloques de búsqueda)
# shown: solo texto, para mostrar; versions: cada respuesta del agente
st.session_state.setdefault("api_messages", [])
st.session_state.setdefault("shown", [])


def run_agent(live):
    """Ejecuta el turno del agente con streaming.

    Sigue mientras la búsqueda web pause el turno. Devuelve el texto posterior al último
    bloque de búsqueda (la respuesta final, sin la narración intermedia) y un aviso si
    el turno se cortó.
    """
    streamed, final_text, searches, notice = "", [], 0, ""
    for _ in range(6):
        with client.messages.stream(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=[WEB_SEARCH],
            messages=st.session_state.api_messages,
        ) as stream:
            for chunk in stream.text_stream:
                streamed += chunk
                live.markdown(streamed + "▌")
            resp = stream.get_final_message()
        st.session_state.api_messages.append({"role": "assistant", "content": resp.content})
        for b in resp.content:
            if b.type == "text":
                final_text.append(b.text)
            else:
                final_text = []
                searches += b.type == "server_tool_use"
        if resp.stop_reason == "pause_turn":
            streamed += "\n\n"
            continue
        if resp.stop_reason == "max_tokens":
            notice = "\n\n⚠️ La respuesta se cortó por longitud. Escribe «continúa» para completarla."
        break
    else:
        notice = "\n\n⚠️ Demasiadas rondas de búsqueda. Escribe «continúa» para terminar la propuesta."
    text = "".join(final_text).strip() or streamed.strip()
    return text + notice, searches


with st.sidebar:
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
            answer, searches = run_agent(live)
        except anthropic.APIError as e:
            st.error(f"Error de la API: {e}")
            st.session_state.api_messages.pop()
            st.session_state.shown.pop()
            st.stop()
        live.markdown(answer)
        if searches:
            st.caption(f"{searches} búsqueda(s) web")
    st.session_state.shown.append({"role": "assistant", "content": answer})
    st.rerun()
