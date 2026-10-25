"""
Eliel Engenharia - Educação na Construção
App Streamlit para gerar aulas técnicas: áudio com voz clonada (ElevenLabs),
legendas sincronizadas (.srt), quadros desenhados passo a passo e pacote .zip.

Rodar:  streamlit run app.py
"""

import base64
import io
import json
import logging
import math
import re
import textwrap
import zipfile
from dataclasses import dataclass
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import requests  # noqa: E402
import streamlit as st  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from PIL import Image  # noqa: E402

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

EMPRESA = "Eliel Ribeiro - Engenharia & Arquitetura LTDA"
RODAPE = f"{EMPRESA} | Tucumã - Pará | CREA-PA 152029785-8"
ELEVEN_URL = "https://api.elevenlabs.io/v1"

CATEGORIAS = [
    "Fundações e Concreto Armado",
    "Estruturas Metálicas & LSF (Light Steel Frame)",
    "Dimensionamento e Alvenaria",
    "Laudos e Inspeções Técnicas (INCRA/Outros)",
]

ESTILOS = {
    "Whiteboard (mão desenhando no quadro)": "whiteboard",
    "Projeção CAD (fundo escuro, traço técnico)": "cad",
}

st.set_page_config(
    page_title="Eliel Engenharia - Educação na Construção",
    page_icon="🏗️",
    layout="wide",
)


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------
def listar_vozes(api_key: str):
    r = requests.get(f"{ELEVEN_URL}/voices", headers={"xi-api-key": api_key}, timeout=30)
    r.raise_for_status()
    return [(v["name"], v["voice_id"]) for v in r.json().get("voices", [])]


def gerar_audio(api_key, voice_id, texto, estabilidade, clareza, estilo_fala, modelo):
    """Gera MP3 + alinhamento por caractere (para as legendas)."""
    url = f"{ELEVEN_URL}/text-to-speech/{voice_id}/with-timestamps"
    payload = {
        "text": texto,
        "model_id": modelo,
        "voice_settings": {
            "stability": estabilidade,
            "similarity_boost": clareza,
            "style": estilo_fala,
            "use_speaker_boost": True,
        },
    }
    r = requests.post(
        url,
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        params={"output_format": "mp3_44100_128"},
        data=json.dumps(payload),
        timeout=180,
    )
    if r.status_code != 200:
        try:
            detalhe = r.json().get("detail", r.text)
        except ValueError:
            detalhe = r.text
        raise RuntimeError(f"ElevenLabs respondeu {r.status_code}: {detalhe}")
    dados = r.json()
    audio = base64.b64decode(dados["audio_base64"])
    alinhamento = dados.get("alignment") or dados.get("normalized_alignment")
    return audio, alinhamento


# ---------------------------------------------------------------------------
# Legendas
# ---------------------------------------------------------------------------
def _tempo_srt(seg: float) -> str:
    ms = int(round(seg * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def legendas_do_alinhamento(alin, max_chars=42):
    """Agrupa as palavras em blocos de legenda usando os tempos da ElevenLabs."""
    chars = alin["characters"]
    ini = alin["character_start_times_seconds"]
    fim = alin["character_end_times_seconds"]

    palavras, atual, t0, t1 = [], "", None, None
    for c, a, b in zip(chars, ini, fim):
        if c.isspace():
            if atual:
                palavras.append((atual, t0, t1))
                atual, t0 = "", None
            continue
        if t0 is None:
            t0 = a
        atual += c
        t1 = b
    if atual:
        palavras.append((atual, t0, t1))

    blocos, texto, b0, b1 = [], "", None, None
    for w, a, b in palavras:
        candidato = f"{texto} {w}".strip()
        if texto and len(candidato) > max_chars:
            blocos.append((texto, b0, b1))
            texto, b0 = w, a
        else:
            texto = candidato
            b0 = a if b0 is None else b0
        b1 = b
        if w.endswith((".", "!", "?")):
            blocos.append((texto, b0, b1))
            texto, b0 = "", None
    if texto:
        blocos.append((texto, b0, b1))
    return blocos


def legendas_estimadas(texto, palavras_por_minuto=150, max_chars=42):
    """Sem áudio: estima tempos pela velocidade média de fala."""
    seg_por_palavra = 60 / palavras_por_minuto
    blocos, t = [], 0.0
    for linha in textwrap.wrap(" ".join(texto.split()), max_chars):
        dur = len(linha.split()) * seg_por_palavra
        blocos.append((linha, t, t + dur))
        t += dur
    return blocos


def montar_srt(blocos):
    partes = []
    for i, (txt, a, b) in enumerate(blocos, 1):
        partes.append(f"{i}\n{_tempo_srt(a)} --> {_tempo_srt(b)}\n{txt}\n")
    return "\n".join(partes)


# ---------------------------------------------------------------------------
# Cálculo da sapata isolada (pré-dimensionamento)
# ---------------------------------------------------------------------------
@dataclass
class Sapata:
    N: float  # kN
    sigma: float  # kPa
    a: float  # pilar, cm (direção L)
    b: float  # pilar, cm (direção B)
    A: float = 0
    L: float = 0
    B: float = 0
    aba: float = 0
    h: float = 0
    h0: float = 0


def arred5(x):
    return math.ceil(x / 5) * 5


def dimensionar_sapata(N, sigma, a, b) -> Sapata:
    s = Sapata(N, sigma, a, b)
    s.A = 1.05 * N / sigma  # m² (5% de peso próprio)
    A_cm2 = s.A * 1e4
    # Abas iguais nas duas direções: L - B = a - b
    d = a - b
    B = (-d + math.sqrt(d * d + 4 * A_cm2)) / 2
    s.B = max(arred5(B), arred5(b + 30), 60)
    s.L = s.B + d
    s.aba = (s.L - a) / 2
    # Sapata rígida (NBR 6118, 22.6.1): h >= (A - ap) / 3
    s.h = max(arred5((s.L - a) / 3), 25)
    s.h0 = max(arred5(s.h / 3), 20)
    return s


def memorial(s: Sapata) -> str:
    tensao_real = 1.05 * s.N / (s.L * s.B / 1e4)
    return textwrap.dedent(f"""\
        MEMORIAL DE PRÉ-DIMENSIONAMENTO - SAPATA ISOLADA
        {EMPRESA}
        Data: {datetime.now():%d/%m/%Y %H:%M}

        DADOS
          Carga no pilar (N) ............ {s.N:.1f} kN
          Tensão admissível do solo ..... {s.sigma:.1f} kPa
          Pilar ......................... {s.a:.0f} x {s.b:.0f} cm

        1) ÁREA DA BASE (com 5% de peso próprio)
          A = 1,05 x N / σadm = 1,05 x {s.N:.1f} / {s.sigma:.1f} = {s.A:.3f} m²

        2) DIMENSÕES EM PLANTA (abas iguais)
          L - B = a - b = {s.a - s.b:.0f} cm
          L = {s.L:.0f} cm   B = {s.B:.0f} cm   (arredondado de 5 em 5 cm)
          Área adotada = {s.L * s.B / 1e4:.3f} m²
          Aba = (L - a)/2 = {s.aba:.1f} cm
          Tensão no solo = {tensao_real:.1f} kPa  (≤ {s.sigma:.1f} kPa: {"OK" if tensao_real <= s.sigma + 0.01 else "VERIFICAR"})

        3) ALTURA - SAPATA RÍGIDA (NBR 6118, item 22.6.1)
          h ≥ (L - a)/3 = {(s.L - s.a) / 3:.1f} cm  →  h = {s.h:.0f} cm
          h0 ≥ h/3 e ≥ 20 cm  →  h0 = {s.h0:.0f} cm
          Lastro de concreto magro: 5 cm

        OBSERVAÇÃO
          Pré-dimensionamento para fins didáticos. O projeto executivo exige
          verificação de punção, armaduras de flexão, ancoragem e cobrimento
          conforme NBR 6118 e NBR 6122.
        """)


# ---------------------------------------------------------------------------
# Desenho passo a passo
# ---------------------------------------------------------------------------
TEMAS = {
    "whiteboard": dict(fundo="#fbfbf7", traco="#1d2a3a", destaque="#c0392b",
                       cota="#1f5fa8", concreto="#d9d4c7", texto="#1d2a3a"),
    "cad": dict(fundo="#0f1720", traco="#e6edf3", destaque="#f5c542",
                cota="#4fd1c5", concreto="#2a3644", texto="#e6edf3"),
}


def _figura(estilo):
    t = TEMAS[estilo]
    if estilo == "whiteboard":
        ctx = plt.xkcd(scale=0.6, length=120, randomness=2)
    else:
        ctx = plt.rc_context({"font.family": "monospace"})
    return t, ctx


def _cota(ax, x1, y1, x2, y2, texto, t, desloc=(0, 0), vertical=False):
    ax.annotate("", xy=(x1, y1), xytext=(x2, y2),
                arrowprops=dict(arrowstyle="<->", color=t["cota"], lw=1.3))
    xm, ym = (x1 + x2) / 2 + desloc[0], (y1 + y2) / 2 + desloc[1]
    ax.text(xm, ym, texto, color=t["cota"], fontsize=11, ha="center", va="center",
            rotation=90 if vertical else 0,
            bbox=dict(fc=t["fundo"], ec="none", pad=1))


def desenhar_passo(s: Sapata, passo: int, estilo: str, cotas: bool, titulo: str):
    """Passo 1: pilar | 2: contorno da sapata | 3: cotas em planta | 4: corte A-A."""
    t, ctx = _figura(estilo)
    with ctx:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 6.75), dpi=110,
                                       gridspec_kw={"width_ratios": [1.1, 1]})
        fig.patch.set_facecolor(t["fundo"])
        for ax in (ax1, ax2):
            ax.set_facecolor(t["fundo"])
            ax.set_aspect("equal")
            ax.axis("off")

        L, B, a, b = s.L, s.B, s.a, s.b
        m = max(L, B) * 0.28

        # ---------- PLANTA ----------
        ax1.set_xlim(-L / 2 - m, L / 2 + m)
        ax1.set_ylim(-B / 2 - m, B / 2 + m)
        ax1.set_title("PLANTA", color=t["texto"], fontsize=14, loc="left")

        if passo >= 2:
            ax1.add_patch(Rectangle((-L / 2, -B / 2), L, B, fc=t["concreto"],
                                    ec=t["traco"], lw=2))
            # linhas das mísulas (diagonais até o pilar)
            for sx in (-1, 1):
                for sy in (-1, 1):
                    ax1.plot([sx * L / 2, sx * a / 2], [sy * B / 2, sy * b / 2],
                             color=t["traco"], lw=1, alpha=0.7)
        ax1.add_patch(Rectangle((-a / 2, -b / 2), a, b, fc=t["destaque"],
                                ec=t["traco"], lw=2, alpha=0.9))
        if passo == 1 or cotas:
            ax1.annotate(f"pilar {a:.0f} × {b:.0f}", xy=(a / 4, b / 2),
                         xytext=(a / 2 + m * 0.2, B / 2 * (passo >= 2) + m * 0.3),
                         color=t["cota"], fontsize=11,
                         arrowprops=dict(arrowstyle="-", color=t["cota"], lw=1))
        if passo >= 3 and cotas:
            y = -B / 2 - m * 0.45
            _cota(ax1, -L / 2, y, L / 2, y, f"L = {L:.0f} cm", t, desloc=(0, -m * 0.12))
            x = -L / 2 - m * 0.45
            _cota(ax1, x, -B / 2, x, B / 2, f"B = {B:.0f} cm", t,
                  desloc=(-m * 0.14, 0), vertical=True)
            _cota(ax1, a / 2, -b / 2 - m * 0.15, L / 2, -b / 2 - m * 0.15,
                  f"aba {s.aba:.0f}", t, desloc=(0, -m * 0.1))
        if passo >= 4:
            ax1.plot([-L / 2 - m * 0.8, L / 2 + m * 0.8], [0, 0], color=t["destaque"],
                     lw=1.2, ls="--")
            ax1.text(L / 2 + m * 0.85, 0, "A", color=t["destaque"], fontsize=13, va="center")
            ax1.text(-L / 2 - m * 0.95, 0, "A", color=t["destaque"], fontsize=13, va="center")

        # ---------- CORTE A-A ----------
        if passo >= 4:
            h, h0, lastro = s.h, s.h0, 5
            arranque = h * 1.2
            ax2.set_xlim(-L / 2 - m, L / 2 + m)
            ax2.set_ylim(-lastro - m * 0.6, h + arranque + m * 0.3)
            ax2.set_title("CORTE A-A", color=t["texto"], fontsize=14, loc="left")
            ax2.add_patch(Rectangle((-L / 2 - 5, -lastro), L + 10, lastro,
                                    fc="none", ec=t["traco"], lw=1, hatch="..."))
            perfil_x = [-L / 2, -L / 2, -a / 2 - 2.5, a / 2 + 2.5, L / 2, L / 2, -L / 2]
            perfil_y = [0, h0, h, h, h0, 0, 0]
            ax2.fill(perfil_x, perfil_y, fc=t["concreto"], ec=t["traco"], lw=2)
            ax2.add_patch(Rectangle((-a / 2, h), a, arranque, fc=t["destaque"],
                                    ec=t["traco"], lw=2, alpha=0.9))
            if cotas:
                x = L / 2 + m * 0.3
                _cota(ax2, x, 0, x, h, f"h={h:.0f}", t, desloc=(m * 0.18, 0), vertical=True)
                x0 = -L / 2 - m * 0.3
                _cota(ax2, x0, 0, x0, h0, f"h0={h0:.0f}", t,
                      desloc=(-m * 0.18, 0), vertical=True)
                ax2.text(0, -lastro - m * 0.3, "lastro de concreto magro e = 5 cm",
                         color=t["texto"], ha="center", fontsize=10)
        else:
            ax2.text(0.5, 0.5, "corte A-A\nno passo 4", color=t["texto"], alpha=0.4,
                     ha="center", va="center", fontsize=16, transform=ax2.transAxes)

        fig.subplots_adjust(left=0.01, right=0.99, top=0.90, bottom=0.10, wspace=0.05)
        fig.suptitle(titulo, color=t["texto"], fontsize=16, x=0.02, ha="left")
        legenda_passo = {
            1: "Passo 1 — Pilar de partida (seção a × b)",
            2: "Passo 2 — Contorno da sapata com abas iguais",
            3: "Passo 3 — Cotas em planta: L, B e abas",
            4: "Passo 4 — Corte A-A: alturas h, h0 e lastro",
        }[passo]
        fig.text(0.02, 0.04, legenda_passo, color=t["destaque"], fontsize=13)
        fig.text(0.98, 0.04, "Eliel Engenharia", color=t["texto"], alpha=0.5,
                 fontsize=10, ha="right")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=t["fundo"])
        plt.close(fig)
    return buf.getvalue()


def quadros_do_roteiro(roteiro: str, estilo: str, titulo: str, frases_por_quadro=2):
    """Transforma o roteiro em quadros de aula (tópicos escritos no quadro)."""
    t, ctx = _figura(estilo)
    frases = [f.strip() for f in re.split(r"(?<=[.!?])\s+", roteiro.strip()) if f.strip()]
    grupos = [frases[i:i + frases_por_quadro] for i in range(0, len(frases), frases_por_quadro)]
    imagens = []
    for n, grupo in enumerate(grupos, 1):
        with ctx:
            fig = plt.figure(figsize=(12, 6.75), dpi=110)
            fig.patch.set_facecolor(t["fundo"])
            fig.text(0.05, 0.88, titulo, color=t["texto"], fontsize=20)
            fig.text(0.05, 0.82, f"Quadro {n} de {len(grupos)}", color=t["destaque"],
                     fontsize=12)
            y = 0.70
            for frase in grupo:
                linhas = textwrap.wrap(frase, 62)
                for i, linha in enumerate(linhas):
                    marcador = "•  " if i == 0 else "   "
                    fig.text(0.06, y, marcador + linha, color=t["texto"], fontsize=17)
                    y -= 0.075
                y -= 0.04
            fig.text(0.98, 0.04, "Eliel Engenharia", color=t["texto"], alpha=0.5,
                     fontsize=10, ha="right")
            buf = io.BytesIO()
            fig.savefig(buf, format="png", facecolor=t["fundo"])
            plt.close(fig)
        imagens.append(buf.getvalue())
    return imagens


def montar_gif(pngs, ms_por_quadro=2500):
    frames = [Image.open(io.BytesIO(p)).convert("P", palette=Image.ADAPTIVE) for p in pngs]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=ms_por_quadro, loop=0)
    return buf.getvalue()


def nome_arquivo(titulo):
    base = re.sub(r"[^a-zA-Z0-9]+", "_", titulo.lower()).strip("_")
    return base[:50] or "aula"


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
def segredo(nome):
    try:
        return st.secrets.get(nome, "")
    except Exception:  # noqa: BLE001  (sem secrets.toml)
        return ""


def liberado() -> bool:
    """Se APP_SENHA estiver nos secrets, pede a senha antes de abrir o app."""
    senha = segredo("APP_SENHA")
    if not senha or st.session_state.get("liberado"):
        return True
    st.title("🏗️ Eliel Engenharia & Arquitetura LTDA")
    digitada = st.text_input("Senha de acesso", type="password")
    if digitada:
        def limpa(s):  # ignora espaços e maiúsculas/minúsculas
            return "".join(s.split()).lower()

        if limpa(digitada) == limpa(senha):
            st.session_state.liberado = True
            st.rerun()
        st.error("Senha incorreta.")
    return False


def main():
    if not liberado():
        return
    st.title("🏗️ Eliel Engenharia & Arquitetura LTDA")
    st.subheader("Módulo: Engenharia e Educação na Construção")
    st.markdown("---")

    # ----- Barra lateral -----
    st.sidebar.header("Painel de Controle")
    categoria = st.sidebar.selectbox("Escolha a Categoria Técnica", CATEGORIAS)

    st.sidebar.markdown("### 🔑 ElevenLabs")
    chave_padrao = segredo("ELEVENLABS_API_KEY")
    api_key = st.sidebar.text_input("Chave da API", value=chave_padrao, type="password",
                                    help="Fica só nesta sessão. Para não digitar sempre, "
                                         "coloque em .streamlit/secrets.toml")
    voz_padrao = segredo("ELEVENLABS_VOICE_ID")
    voice_id = voz_padrao
    if api_key:
        try:
            if "vozes" not in st.session_state or st.session_state.get("_key") != api_key:
                st.session_state.vozes = listar_vozes(api_key)
                st.session_state._key = api_key
            vozes = st.session_state.vozes
            if vozes:
                nomes = [n for n, _ in vozes]
                ids = [i for _, i in vozes]
                idx = ids.index(voz_padrao) if voz_padrao in ids else 0
                escolha = st.sidebar.selectbox("Sua voz clonada", nomes, index=idx)
                voice_id = ids[nomes.index(escolha)]
            else:
                st.sidebar.warning("Nenhuma voz encontrada nessa conta.")
        except Exception as e:  # noqa: BLE001
            st.sidebar.error(f"Não consegui listar as vozes: {e}")
            voice_id = st.sidebar.text_input("Voice ID (manual)", value=voz_padrao)

    modelo = st.sidebar.selectbox(
        "Modelo de voz",
        ["eleven_multilingual_v2", "eleven_turbo_v2_5", "eleven_flash_v2_5"],
        help="multilingual_v2 tem a melhor qualidade em português.",
    )

    # ----- Área principal -----
    st.markdown(f"### 📚 Tema Atual: {categoria}")
    col1, col2 = st.columns([1, 1])

    with col1:
        st.markdown("#### 1. Roteiro e Parâmetros Técnicos")
        titulo_aula = st.text_input("Título do Vídeo / Aula",
                                    "Dimensionamento de Sapata Isolada - NBR 6118")
        roteiro = st.text_area(
            "Roteiro da narração (será lido com a sua voz):",
            value=("Fala, engenheiro! Aqui é o Eliel, da Eliel Engenharia. "
                   "Hoje vamos direto ao ponto crítico de qualquer obra: a fundação em "
                   "concreto armado. Primeiro, a gente parte da carga do pilar e da tensão "
                   "admissível do solo. Com isso calculamos a área da base, sempre somando "
                   "cerca de cinco por cento de peso próprio. Depois definimos as dimensões "
                   "com abas iguais, para a sapata trabalhar de forma equilibrada. Por fim, "
                   "verificamos a altura pelo critério de sapata rígida da NBR 6118."),
            height=220,
        )
        palavras = len(roteiro.split())
        st.caption(f"{palavras} palavras · {len(roteiro)} caracteres · "
                   f"≈ {palavras / 150:.1f} min de narração")

        st.markdown("#### 🎙️ Configuração de Voz Clonada")
        estabilidade = st.slider("Estabilidade do timbre", 0.0, 1.0, 0.75,
                                 help="Mais alto = voz mais constante; mais baixo = mais expressiva.")
        clareza = st.slider("Fidelidade à sua voz (clareza e sotaque)", 0.0, 1.0, 0.85)
        estilo_fala = st.slider("Ênfase / estilo", 0.0, 1.0, 0.0,
                                help="Acima de 0 deixa a fala mais enfática, mas pode "
                                     "reduzir a estabilidade.")

    sapata = None
    with col2:
        st.markdown("#### 2. Formalização Desenhada (Visual Passo a Passo)")
        estilo_nome = st.selectbox("Estilo da animação na tela", list(ESTILOS))
        estilo = ESTILOS[estilo_nome]
        incluir_cotas = st.checkbox("Incluir cotas nos desenhos", value=True)
        gerar_legenda = st.checkbox("Gerar legendas (.srt) para o vídeo", value=True)
        gerar_voz = st.checkbox("Gerar áudio com a voz clonada", value=True)

        if categoria == "Fundações e Concreto Armado":
            with st.expander("📐 Dados da sapata isolada", expanded=True):
                c1, c2 = st.columns(2)
                N = c1.number_input("Carga no pilar N (kN)", 10.0, 20000.0, 800.0, 10.0)
                sigma = c2.number_input("Tensão admissível (kPa)", 50.0, 2000.0, 200.0, 10.0)
                a = c1.number_input("Pilar a (cm)", 12.0, 200.0, 40.0, 1.0)
                b = c2.number_input("Pilar b (cm)", 12.0, 200.0, 20.0, 1.0)
                sapata = dimensionar_sapata(N, sigma, a, b)
                st.success(f"Sapata {sapata.L:.0f} × {sapata.B:.0f} cm · "
                           f"h = {sapata.h:.0f} cm · h0 = {sapata.h0:.0f} cm")
                st.image(desenhar_passo(sapata, 4, estilo, incluir_cotas, titulo_aula),
                         caption="Prévia do último passo")
        else:
            st.info("Para esta categoria, os quadros são montados a partir do roteiro "
                    "(tópicos escritos no quadro). O desenho técnico automático está "
                    "disponível em Fundações e Concreto Armado.")

        st.markdown("---")
        processar = st.button("🚀 Processar e Gerar Aula Completa", type="primary",
                              use_container_width=True)

    if processar:
        if not roteiro.strip():
            st.warning("Escreva o roteiro antes de gerar a aula.")
            return
        if gerar_voz and (not api_key or not voice_id):
            st.warning("Informe a chave da ElevenLabs e escolha a voz na barra lateral, "
                       "ou desmarque “Gerar áudio com a voz clonada”.")
            return

        arquivos = {}
        base = nome_arquivo(titulo_aula)
        progresso = st.progress(0, text="Preparando…")

        audio, alin = None, None
        if gerar_voz:
            progresso.progress(10, text="Gerando narração com a sua voz…")
            try:
                audio, alin = gerar_audio(api_key, voice_id, roteiro, estabilidade,
                                          clareza, estilo_fala, modelo)
                arquivos[f"{base}_narracao.mp3"] = audio
            except Exception as e:  # noqa: BLE001
                st.error(f"Falha ao gerar o áudio: {e}")
                return

        if gerar_legenda:
            progresso.progress(40, text="Montando legendas…")
            blocos = legendas_do_alinhamento(alin) if alin else legendas_estimadas(roteiro)
            arquivos[f"{base}_legendas.srt"] = montar_srt(blocos).encode("utf-8")

        progresso.progress(55, text="Desenhando os quadros passo a passo…")
        quadros = quadros_do_roteiro(roteiro, estilo, titulo_aula)
        desenhos = []
        if sapata:
            desenhos = [desenhar_passo(sapata, p, estilo, incluir_cotas, titulo_aula)
                        for p in range(1, 5)]
            arquivos[f"{base}_memorial.txt"] = memorial(sapata).encode("utf-8")
        todos = quadros + desenhos
        for i, png in enumerate(todos, 1):
            arquivos[f"quadros/{i:02d}.png"] = png

        progresso.progress(80, text="Animando a sequência…")
        arquivos[f"{base}_animacao.gif"] = montar_gif(todos)
        arquivos[f"{base}_roteiro.txt"] = roteiro.encode("utf-8")

        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            for nome, dados in arquivos.items():
                z.writestr(nome, dados)
        progresso.progress(100, text="Pronto!")

        st.balloons()
        st.markdown("### ✅ Aula pronta para exportação")
        if audio:
            st.audio(audio, format="audio/mp3")
        st.image(arquivos[f"{base}_animacao.gif"], caption="Sequência dos quadros")
        if sapata:
            with st.expander("Memorial de cálculo"):
                st.code(memorial(sapata), language=None)
        st.download_button(
            "📥 Baixar pacote da aula (.zip)",
            data=zbuf.getvalue(),
            file_name=f"{base}.zip",
            mime="application/zip",
            type="primary",
        )
        st.caption("O pacote traz: narração .mp3, legendas .srt, quadros .png, "
                   "animação .gif, roteiro e memorial. Monte o vídeo no CapCut ou "
                   "Premiere: importe os quadros, o áudio e as legendas.")

    st.markdown("---")
    st.caption(RODAPE)


if __name__ == "__main__":
    main()
