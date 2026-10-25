"""
Vídeo estilo "quadro branco" (whiteboard) para Reels/Instagram — 1080x1920.

- Desenhos e contas são riscados aos poucos, no ritmo da narração.
- Uma mão com caneta acompanha a ponta do traço.
- A foto/caricatura do professor fica num selo redondo e "pulsa" quando ele fala.
- Legenda grande na parte de baixo, sincronizada com a voz.
"""

import io
import math
import os
import random
import re
import subprocess
import tempfile
import textwrap

import numpy as np
from PIL import Image, ImageDraw, ImageFont

AQUI = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(AQUI, "assets") if os.path.isdir(os.path.join(AQUI, "assets")) else AQUI

W, H = 1080, 1920
FPS = 24

# Área do quadro dentro do vídeo
QX, QY, QW, QH = 40, 330, 1000, 1060

COR_FUNDO = (250, 250, 246)
COR_QUADRO = (255, 255, 255)
COR_TINTA = (29, 42, 58)        # azul-marinho (caneta preta)
COR_AZUL = (31, 95, 168)        # caneta azul (cotas)
COR_VERM = (192, 57, 43)        # caneta vermelha (destaques)
COR_VERDE = (33, 128, 76)
COR_CONCRETO = (222, 216, 202)
COR_FAIXA = (22, 34, 82)
COR_OURO = (245, 197, 66)


# ---------------------------------------------------------------------------
# Fontes e imagens
# ---------------------------------------------------------------------------
def fonte_mao(tam):
    try:
        return ImageFont.truetype(os.path.join(ASSETS, "PatrickHand-Regular.ttf"), tam)
    except OSError:
        return fonte_sans(tam)


def fonte_sans(tam, negrito=True):
    import matplotlib
    nome = "DejaVuSans-Bold.ttf" if negrito else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(os.path.join(matplotlib.get_data_path(), "fonts", "ttf", nome),
                                  tam)
    except OSError:
        return ImageFont.load_default()


def carregar_mao(altura=230):
    caminho = os.path.join(ASSETS, "mao.png")
    if not os.path.exists(caminho):
        return None, (0, 0)
    mao = Image.open(caminho).convert("RGBA")
    esc = altura / mao.height
    mao = mao.resize((int(mao.width * esc), altura), Image.LANCZOS)
    # ponta da caneta fica no canto inferior esquerdo da imagem (≈ 5%, 96%)
    ponta = (int(mao.width * 0.05), int(mao.height * 0.96))
    return mao, ponta


def selo_redondo(foto_bytes, diam):
    """Foto recortada em círculo com borda branca e anel azul."""
    foto = Image.open(io.BytesIO(foto_bytes)).convert("RGB")
    lado = min(foto.size)
    esq, topo = (foto.width - lado) // 2, (foto.height - lado) // 2
    foto = foto.crop((esq, topo, esq + lado, topo + lado)).resize((diam, diam), Image.LANCZOS)
    mascara = Image.new("L", (diam, diam), 0)
    ImageDraw.Draw(mascara).ellipse((0, 0, diam - 1, diam - 1), fill=255)
    borda = 14
    total = diam + 2 * borda
    selo = Image.new("RGBA", (total, total), (0, 0, 0, 0))
    d = ImageDraw.Draw(selo)
    d.ellipse((0, 0, total - 1, total - 1), fill=COR_FAIXA + (255,))
    d.ellipse((6, 6, total - 7, total - 7), fill=(255, 255, 255, 255))
    selo.paste(foto, (borda, borda), mascara)
    return selo


# ---------------------------------------------------------------------------
# Elementos que a "mão" desenha
# ---------------------------------------------------------------------------
class Traco:
    """Linha à mão livre (polilinha com leve tremido)."""

    def __init__(self, pts, cor=COR_TINTA, larg=6, tremido=1.6, semente=0):
        rnd = random.Random(semente)
        self.pts = []
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            dist = math.hypot(x2 - x1, y2 - y1)
            n = max(2, int(dist / 18))
            for i in range(n):
                t = i / n
                self.pts.append((x1 + (x2 - x1) * t + rnd.uniform(-tremido, tremido),
                                 y1 + (y2 - y1) * t + rnd.uniform(-tremido, tremido)))
        self.pts.append(pts[-1])
        self.cor, self.larg = cor, larg
        self.cum = [0.0]
        for (a, b), (c, d) in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + math.hypot(c - a, d - b))
        self.trabalho = max(self.cum[-1], 1.0)

    def desenhar(self, d, frac):
        alvo = self.trabalho * frac
        pts = [self.pts[0]]
        ponta = self.pts[0]
        for i in range(1, len(self.pts)):
            if self.cum[i] <= alvo:
                pts.append(self.pts[i])
                ponta = self.pts[i]
            else:
                seg = self.cum[i] - self.cum[i - 1]
                t = (alvo - self.cum[i - 1]) / seg if seg else 1
                (a, b), (c, e) = self.pts[i - 1], self.pts[i]
                ponta = (a + (c - a) * t, b + (e - b) * t)
                pts.append(ponta)
                break
        if len(pts) > 1:
            d.line(pts, fill=self.cor, width=self.larg, joint="curve")
        return ponta


_CMAP = None


def _tem_glifo(ch):
    global _CMAP
    if _CMAP is None:
        try:
            from fontTools.ttLib import TTFont
            _CMAP = set(TTFont(os.path.join(ASSETS, "PatrickHand-Regular.ttf")).getBestCmap())
        except Exception:  # noqa: BLE001
            _CMAP = set(range(32, 127)) | set(range(160, 256))
    return ord(ch) in _CMAP or ch.isspace()


class Texto:
    """Texto escrito letra por letra (letra de mão; símbolos que faltam usam outra fonte)."""

    def __init__(self, x, y, txt, tam=56, cor=COR_TINTA, centro=False):
        self.fm = fonte_mao(tam)
        self.fs = fonte_sans(int(tam * 0.85), negrito=False)
        self.txt, self.cor, self.tam = txt, cor, tam
        self.y = y
        largura = self._largura(txt)
        self.x = x - largura / 2 if centro else x
        self.trabalho = max(len(txt), 1) * 16.0

    def _runs(self, txt):
        runs = []
        for ch in txt:
            f = self.fm if _tem_glifo(ch) else self.fs
            if runs and runs[-1][1] is f:
                runs[-1][0] += ch
            else:
                runs.append([ch, f])
        return runs

    def _largura(self, txt):
        d = ImageDraw.Draw(Image.new("L", (1, 1)))
        return sum(d.textlength(t, font=f) for t, f in self._runs(txt))

    def desenhar(self, d, frac):
        n = int(round(len(self.txt) * frac))
        x = self.x
        for t, f in self._runs(self.txt[:n]):
            dy = self.tam * 0.18 if f is self.fs else 0
            d.text((x, self.y + dy), t, font=f, fill=self.cor)
            x += d.textlength(t, font=f)
        return (x, self.y + self.tam * 0.85)


class Preenchimento:
    """Área pintada (aparece de uma vez quando chega a vez dela)."""

    def __init__(self, poligono, cor=COR_CONCRETO):
        self.pol, self.cor = poligono, cor
        self.trabalho = 60.0

    def desenhar(self, d, frac):
        if frac > 0:
            d.polygon(self.pol, fill=self.cor)
        xs = [p[0] for p in self.pol]
        ys = [p[1] for p in self.pol]
        return (min(xs) + (max(xs) - min(xs)) * frac, max(ys))


def retangulo(x0, y0, x1, y1, **kw):
    return Traco([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], **kw)


def cota(x0, y0, x1, y1, rotulo, desloc=(0, 0), tam=44, sem=0):
    """Linha de cota com setas + texto."""
    itens = [Traco([(x0, y0), (x1, y1)], cor=COR_AZUL, larg=4, tremido=0.8, semente=sem)]
    ang = math.atan2(y1 - y0, x1 - x0)
    for (px, py), sinal in (((x0, y0), 1), ((x1, y1), -1)):
        for da in (0.45, -0.45):
            a = ang + da
            itens.append(Traco([(px, py), (px + sinal * 26 * math.cos(a),
                                           py + sinal * 26 * math.sin(a))],
                               cor=COR_AZUL, larg=4, tremido=0.3, semente=sem + 1))
    xm, ym = (x0 + x1) / 2 + desloc[0], (y0 + y1) / 2 + desloc[1]
    itens.append(Texto(xm, ym - tam / 2, rotulo, tam=tam, cor=COR_AZUL, centro=True))
    return itens


# ---------------------------------------------------------------------------
# Cenas
# ---------------------------------------------------------------------------
def br(v, casas=2):
    return f"{v:.{casas}f}".replace(".", ",")


def cenas_sapata(s):
    """Cenas do pré-dimensionamento da sapata isolada (objeto Sapata do app)."""
    cenas = []

    # 1) Dados
    cenas.append([
        Texto(500, 60, "Dados do problema", tam=78, cor=COR_VERM, centro=True),
        Traco([(250, 160), (750, 160)], cor=COR_VERM, larg=5, semente=1),
        Texto(140, 260, f"Carga no pilar:  N = {br(s.N, 0)} kN", tam=60),
        Texto(140, 380, f"Solo:  σadm = {br(s.sigma, 0)} kPa", tam=60),
        Texto(140, 500, f"Pilar:  {br(s.a, 0)} × {br(s.b, 0)} cm", tam=60),
        retangulo(420, 680, 420 + s.a * 4, 680 + s.b * 4, cor=COR_VERM, larg=7, semente=2),
        Texto(500, 650 + s.b * 4 + 60, "pilar (seção)", tam=46, cor=COR_VERM, centro=True),
    ])

    # 2) Área
    cenas.append([
        Texto(500, 60, "1. Área da base", tam=78, cor=COR_VERM, centro=True),
        Traco([(270, 160), (730, 160)], cor=COR_VERM, larg=5, semente=3),
        Texto(110, 250, "A = 1,05 · N / σadm", tam=70),
        Texto(110, 370, f"A = 1,05 · {br(s.N, 0)} / {br(s.sigma, 0)}", tam=70),
        Texto(110, 490, f"A = {br(s.A)} m²", tam=84, cor=COR_AZUL),
        Texto(110, 650, "Abas iguais:  L − B = a − b", tam=60),
        Texto(110, 760, f"L = {br(s.L, 0)} cm    B = {br(s.B, 0)} cm", tam=70, cor=COR_AZUL),
        Traco([(100, 870), (900, 870)], cor=COR_VERDE, larg=4, semente=4),
        Texto(110, 890, f"✔ {br(s.L * s.B / 1e4)} m² ≥ {br(s.A)} m²", tam=56, cor=COR_VERDE),
    ])

    # 3) Planta
    esc = 480 / max(s.L, s.B)
    L, B, a, b = s.L * esc, s.B * esc, s.a * esc, s.b * esc
    cx, cy = 500, 520
    x0, y0, x1, y1 = cx - L / 2, cy - B / 2, cx + L / 2, cy + B / 2
    planta = [
        Texto(500, 40, "2. Planta da sapata", tam=72, cor=COR_VERM, centro=True),
        Preenchimento([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]),
        retangulo(x0, y0, x1, y1, larg=7, semente=5),
    ]
    for i, (sx, sy) in enumerate(((-1, -1), (1, -1), (1, 1), (-1, 1))):
        planta.append(Traco([(cx + sx * L / 2, cy + sy * B / 2), (cx + sx * a / 2, cy + sy * b / 2)],
                            larg=3, semente=10 + i))
    planta += [
        Preenchimento([(cx - a / 2, cy - b / 2), (cx + a / 2, cy - b / 2),
                       (cx + a / 2, cy + b / 2), (cx - a / 2, cy + b / 2)], cor=COR_VERM),
        Traco([(x0 - 50, cy), (x1 + 40, cy)], cor=COR_VERM, larg=3, tremido=0.5, semente=20),
        Texto(x0 - 70, cy - 70, "A", tam=50, cor=COR_VERM),
        Texto(x1 + 15, cy - 70, "A", tam=50, cor=COR_VERM),
    ]
    planta += cota(x0, y1 + 70, x1, y1 + 70, f"L = {br(s.L, 0)} cm", desloc=(0, 45), sem=30)
    planta += cota(x1 + 95, y0, x1 + 95, y1, f"B = {br(s.B, 0)}", desloc=(75, 0), sem=40)
    planta += cota(cx + a / 2, cy + 60, x1, cy + 60, f"aba {br(s.aba, 0)}", desloc=(0, 40),
                   tam=38, sem=50)
    cenas.append(planta)

    # 4) Corte
    esc2 = 600 / s.L
    Lc, ac = s.L * esc2, s.a * esc2
    h, h0 = max(s.h * esc2, 90), max(s.h0 * esc2, 35)
    base = 700
    xa, xb = 500 - Lc / 2, 500 + Lc / 2
    perfil = [(xa, base), (xa, base - h0), (500 - ac / 2 - 6, base - h),
              (500 + ac / 2 + 6, base - h), (xb, base - h0), (xb, base), (xa, base)]
    corte = [
        Texto(500, 40, "3. Corte A-A", tam=72, cor=COR_VERM, centro=True),
        Preenchimento(perfil[:-1]),
        Traco(perfil, larg=7, semente=60),
        Preenchimento([(500 - ac / 2, base - h), (500 + ac / 2, base - h),
                       (500 + ac / 2, base - h - 230), (500 - ac / 2, base - h - 230)],
                      cor=COR_VERM),
        retangulo(xa - 15, base, xb + 15, base + 26, larg=3, semente=61),
    ]
    corte += cota(xb + 60, base, xb + 60, base - h, f"h={br(s.h, 0)}", desloc=(70, 0),
                  tam=40, sem=70)
    corte += cota(xa - 60, base, xa - 60, base - h0, f"h0={br(s.h0, 0)}", desloc=(-80, 0),
                  tam=40, sem=80)
    corte += [
        Texto(500, base + 50, "lastro de concreto magro (5 cm)", tam=42, centro=True),
        Texto(500, base + 150, f"Rígida:  h ≥ (L − a)/3 = {br((s.L - s.a) / 3, 0)} cm",
              tam=54, cor=COR_AZUL, centro=True),
    ]
    cenas.append(corte)

    # 5) Resumo
    tensao = 1.05 * s.N / (s.L * s.B / 1e4)
    cenas.append([
        Texto(500, 80, "Resultado", tam=90, cor=COR_VERM, centro=True),
        Traco([(330, 200), (670, 200)], cor=COR_VERM, larg=6, semente=90),
        Texto(500, 300, f"Sapata {br(s.L, 0)} × {br(s.B, 0)} cm", tam=84, centro=True),
        Texto(500, 430, f"h = {br(s.h, 0)} cm    h0 = {br(s.h0, 0)} cm", tam=72, centro=True),
        Texto(500, 580, f"Tensão no solo: {br(tensao, 1)} kPa", tam=60, cor=COR_AZUL, centro=True),
        Texto(500, 680, f"✔ ≤ {br(s.sigma, 0)} kPa  OK!", tam=70, cor=COR_VERDE, centro=True),
        Traco([(250, 860), (750, 860)], cor=COR_OURO, larg=8, semente=91),
        Texto(500, 880, "Pré-dimensionamento didático", tam=44, centro=True),
    ])
    return cenas


def cenas_texto(roteiro, titulo):
    """Para temas sem desenho automático: tópicos do roteiro escritos no quadro."""
    frases = [f.strip() for f in re.split(r"(?<=[.!?])\s+", roteiro.strip()) if f.strip()]
    cenas, grupo = [], []
    for fr in frases:
        grupo.append(fr)
        if len(" ".join(grupo)) > 140:
            cenas.append(grupo)
            grupo = []
    if grupo:
        cenas.append(grupo)
    saida = []
    for n, grupo in enumerate(cenas, 1):
        itens = [Texto(500, 50, titulo if n == 1 else f"{titulo} ({n})", tam=60,
                       cor=COR_VERM, centro=True)]
        y = 190
        for fr in grupo:
            for i, linha in enumerate(textwrap.wrap(fr, 30)):
                itens.append(Texto(70, y, ("• " if i == 0 else "   ") + linha, tam=62))
                y += 90
            y += 30
            if y > 900:
                break
        saida.append(itens)
    return saida


# ---------------------------------------------------------------------------
# Montagem do vídeo
# ---------------------------------------------------------------------------
def _quebra(d, texto, fonte, largura):
    linhas, atual = [], ""
    for p in texto.split():
        t = f"{atual} {p}".strip()
        if d.textlength(t, font=fonte) <= largura:
            atual = t
        else:
            if atual:
                linhas.append(atual)
            atual = p
    if atual:
        linhas.append(atual)
    return linhas


def _fundo(titulo, rodape):
    img = Image.new("RGB", (W, H), COR_FUNDO)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 290), fill=COR_FAIXA)
    d.text((50, 60), "ELIEL ENGENHARIA", font=fonte_sans(38), fill=COR_OURO)
    f = fonte_sans(52)
    y = 120
    for linha in _quebra(d, titulo, f, W - 100)[:2]:
        d.text((50, y), linha, font=f, fill=(255, 255, 255))
        y += 64
    # moldura do quadro
    d.rounded_rectangle((QX - 12, QY - 12, QX + QW + 12, QY + QH + 12), radius=18,
                        fill=(190, 196, 204))
    d.rounded_rectangle((QX - 4, QY - 4, QX + QW + 4, QY + QH + 4), radius=12,
                        fill=COR_QUADRO)
    d.text((50, H - 90), rodape, font=fonte_sans(30, False), fill=(110, 120, 132))
    return img


def _camada_legenda(texto, largura):
    f = fonte_sans(54)
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    linhas = _quebra(tmp, texto, f, largura - 40)[:3]
    alt = 30 + 70 * len(linhas)
    cam = Image.new("RGBA", (largura, alt), (0, 0, 0, 0))
    d = ImageDraw.Draw(cam)
    d.rounded_rectangle((0, 0, largura, alt), radius=24, fill=COR_FAIXA + (235,))
    y = 15
    for ln in linhas:
        w = d.textlength(ln, font=f)
        d.text(((largura - w) / 2, y), ln, font=f, fill=(255, 255, 255))
        y += 70
    return cam


def _volume_audio(ffmpeg, mp3_path, dur):
    """Volume da voz por frame (0..1), para o selo pulsar quando fala."""
    try:
        r = subprocess.run([ffmpeg, "-loglevel", "error", "-i", mp3_path, "-ac", "1", "-ar",
                            "8000", "-f", "s16le", "-"], capture_output=True, check=True)
        amostras = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)
        por = int(8000 / FPS)
        n = int(dur * FPS) + 1
        vol = np.zeros(n)
        for i in range(n):
            bloco = amostras[i * por:(i + 1) * por]
            if len(bloco):
                vol[i] = np.sqrt(np.mean(bloco ** 2))
        if vol.max() > 0:
            vol = vol / np.percentile(vol[vol > 0], 95)
        return np.clip(vol, 0, 1)
    except Exception:  # noqa: BLE001
        return None


def gerar_video_quadro(cenas, blocos, duracao, audio, titulo, foto_bytes=None,
                       rodape="CREA-PA 152029785-8 · Tucumã - PA", progresso=None):
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    fundo = _fundo(titulo, rodape)
    mao, ponta_mao = carregar_mao()
    selo = selo_redondo(foto_bytes, 260) if foto_bytes else None
    larg_leg = W - 80 - (300 if selo else 0)

    # tempo de cada cena proporcional ao "trabalho" de desenho
    trab = [sum(it.trabalho for it in c) for c in cenas]
    total = sum(trab)
    tempos = [max(2.5, duracao * t / total) for t in trab]
    fator = duracao / sum(tempos)
    tempos = [t * fator for t in tempos]
    inicios = np.cumsum([0] + tempos[:-1])

    legendas = {}
    n_frames = int(duracao * FPS) + 1

    with tempfile.TemporaryDirectory() as tmp:
        vol = None
        mp3 = None
        if audio:
            mp3 = os.path.join(tmp, "narracao.mp3")
            with open(mp3, "wb") as f:
                f.write(audio)
            vol = _volume_audio(ffmpeg, mp3, duracao)
        saida = os.path.join(tmp, "aula.mp4")
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
        if mp3:
            cmd += ["-i", mp3, "-c:a", "aac", "-b:a", "160k", "-shortest"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", saida]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        cena_atual, quadro, feitos = -1, None, 0
        try:
            for fi in range(n_frames):
                t = fi / FPS
                ci = int(np.searchsorted(inicios, t, side="right") - 1)
                ci = max(0, min(ci, len(cenas) - 1))
                if ci != cena_atual:
                    cena_atual, feitos = ci, 0
                    quadro = Image.new("RGB", (QW, QH), COR_QUADRO)
                itens = cenas[ci]
                # desenha em 80% do tempo da cena; o resto fica parado para leitura
                frac = min(1.0, (t - inicios[ci]) / (tempos[ci] * 0.8))
                alvo = frac * sum(it.trabalho for it in itens)
                d = ImageDraw.Draw(quadro)
                acum = sum(it.trabalho for it in itens[:feitos])
                while feitos < len(itens) and acum + itens[feitos].trabalho <= alvo:
                    itens[feitos].desenhar(d, 1.0)
                    acum += itens[feitos].trabalho
                    feitos += 1
                frame = fundo.copy()
                parcial = quadro
                ponta = None
                if feitos < len(itens):
                    parcial = quadro.copy()
                    fr_item = (alvo - acum) / itens[feitos].trabalho
                    ponta = itens[feitos].desenhar(ImageDraw.Draw(parcial), max(fr_item, 0.0))
                frame.paste(parcial, (QX, QY))

                if mao is not None and ponta is not None:
                    hx = int(QX + ponta[0] - ponta_mao[0])
                    hy = int(QY + ponta[1] - ponta_mao[1])
                    frame.paste(mao, (hx, hy), mao)

                leg = next((i for i, (_, a, b) in enumerate(blocos) if a <= t < b), None)
                if leg is not None:
                    if leg not in legendas:
                        legendas[leg] = _camada_legenda(blocos[leg][0], larg_leg)
                    cam = legendas[leg]
                    frame.paste(cam, (40, QY + QH + 40), cam)

                if selo is not None:
                    v = float(vol[min(fi, len(vol) - 1)]) if vol is not None else 0.0
                    esc = 1.0 + 0.07 * v
                    s = selo.resize((int(selo.width * esc), int(selo.height * esc)),
                                    Image.BILINEAR) if v > 0.05 else selo
                    cx, cy = W - 40 - selo.width // 2, QY + QH + 40 + selo.height // 2
                    frame.paste(s, (cx - s.width // 2, cy - s.height // 2), s)

                proc.stdin.write(frame.tobytes())
                if progresso and fi % FPS == 0:
                    progresso(fi / n_frames)
        finally:
            proc.stdin.close()
            erro = proc.stderr.read().decode(errors="ignore")
            proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(erro[-500:])
        with open(saida, "rb") as f:
            return f.read()
