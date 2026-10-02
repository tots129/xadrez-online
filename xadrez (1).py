"""Xadrez Online: salas com código, feito com Streamlit + python-chess.

Como funciona:
- Todas as salas ficam num dicionário em memória, compartilhado entre todos os
  visitantes do app (st.cache_resource). Por isso duas pessoas em PCs diferentes
  enxergam a mesma partida.
- Um "vigia" (fragmento que roda a cada 2 s) confere se a sala mudou e, se sim,
  redesenha a página. Assim o lance do adversário aparece sozinho.
"""
import json
import random
import tempfile
import threading
import time
import uuid
from pathlib import Path

import chess
import chess.svg
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Xadrez Online", page_icon="♟️", layout="centered")

# O Streamlit declara a página como inglês; como o texto é em português, o Chrome
# oferece traduzir e isso quebra o React ("removeChild") quando o texto muda.
# Este script diz ao navegador que a página já está em português e não deve ser traduzida.
components.html(
    """
    <script>
    try {
      const d = window.parent.document;
      d.documentElement.setAttribute('lang', 'pt-BR');
      d.documentElement.setAttribute('translate', 'no');
      d.body.classList.add('notranslate');
      if (!d.querySelector('meta[name="google"][content="notranslate"]')) {
        const m = d.createElement('meta');
        m.name = 'google';
        m.content = 'notranslate';
        d.head.appendChild(m);
      }
    } catch (e) {}
    </script>
    """,
    height=0,
)

SALA_TTL = 12 * 3600  # salas sem atividade por 12 h são apagadas
INTERVALO = 2  # segundos entre verificações de novidades
ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sem 0/O, 1/I para evitar confusão

NOME_PECA = {
    chess.PAWN: "Peão",
    chess.KNIGHT: "Cavalo",
    chess.BISHOP: "Bispo",
    chess.ROOK: "Torre",
    chess.QUEEN: "Dama",
    chess.KING: "Rei",
}
NOME_COR = {chess.WHITE: "Brancas", chess.BLACK: "Pretas"}
ICONE_COR = {chess.WHITE: "⚪", chess.BLACK: "⚫"}
PROMOCOES = [chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT]
MOTIVOS = {
    chess.Termination.CHECKMATE: "Xeque-mate",
    chess.Termination.STALEMATE: "Afogamento",
    chess.Termination.INSUFFICIENT_MATERIAL: "Material insuficiente",
    chess.Termination.SEVENTYFIVE_MOVES: "Regra dos 75 lances",
    chess.Termination.FIVEFOLD_REPETITION: "Repetição de posição (5x)",
    chess.Termination.FIFTY_MOVES: "Regra dos 50 lances",
    chess.Termination.THREEFOLD_REPETITION: "Repetição de posição (3x)",
}


# ----------------------------------------------------------------------------
# Estado compartilhado (vive no servidor, vale para todos os visitantes)
# ----------------------------------------------------------------------------
@st.cache_resource
def obter_loja():
    return {"salas": {}, "lock": threading.RLock()}


LOJA = obter_loja()


def _tocar(sala):
    """Marca que a sala mudou (o vigia dos outros jogadores percebe isso)."""
    sala["versao"] += 1
    sala["atualizada"] = time.time()


def _limpar_salas_antigas():
    agora = time.time()
    velhas = [c for c, s in LOJA["salas"].items() if agora - s["atualizada"] > SALA_TTL]
    for c in velhas:
        del LOJA["salas"][c]


def _papel(sala, token):
    """Devolve a cor do jogador dono do token, ou None (espectador)."""
    if not token:
        return None
    for cor, j in sala["jogadores"].items():
        if j and j["token"] == token:
            return cor
    return None


def _verificar_fim(sala):
    fim = sala["board"].outcome(claim_draw=True)
    if fim:
        sala["resultado"] = (fim.result(), MOTIVOS.get(fim.termination, "Fim de jogo"))


def criar_sala(nome, preferencia):
    with LOJA["lock"]:
        _limpar_salas_antigas()
        while True:
            codigo = "".join(random.choices(ALFABETO, k=5))
            if codigo not in LOJA["salas"]:
                break
        if preferencia == "Brancas":
            cor = chess.WHITE
        elif preferencia == "Pretas":
            cor = chess.BLACK
        else:
            cor = random.choice([chess.WHITE, chess.BLACK])
        token = uuid.uuid4().hex
        sala = {
            "board": chess.Board(),
            "jogadores": {chess.WHITE: None, chess.BLACK: None},
            "resultado": None,  # (placar, motivo)
            "oferta_empate": None,  # cor de quem ofereceu
            "versao": 0,
            "partidas": 1,
            "atualizada": time.time(),
        }
        sala["jogadores"][cor] = {"token": token, "nome": nome}
        LOJA["salas"][codigo] = sala
        return codigo, token


def entrar_sala(codigo, nome):
    """Devolve (status, token): status = 'inexistente' | 'jogador' | 'cheia'."""
    with LOJA["lock"]:
        sala = LOJA["salas"].get(codigo)
        if sala is None:
            return "inexistente", None
        for cor in (chess.WHITE, chess.BLACK):
            if sala["jogadores"][cor] is None:
                token = uuid.uuid4().hex
                sala["jogadores"][cor] = {"token": token, "nome": nome}
                _tocar(sala)
                return "jogador", token
        return "cheia", None


def snapshot(codigo, token):
    with LOJA["lock"]:
        s = LOJA["salas"].get(codigo)
        if s is None:
            return None
        return {
            "versao": s["versao"],
            "board": s["board"].copy(),
            "nomes": {c: (j["nome"] if j else None) for c, j in s["jogadores"].items()},
            "resultado": s["resultado"],
            "oferta": s["oferta_empate"],
            "partidas": s["partidas"],
            "cor": _papel(s, token),
        }


def acao_jogar(codigo, token, uci):
    with LOJA["lock"]:
        sala = LOJA["salas"].get(codigo)
        if sala is None or sala["resultado"]:
            return False
        cor = _papel(sala, token)
        board = sala["board"]
        if cor is None or board.turn != cor:
            return False
        if not all(sala["jogadores"].values()):
            return False
        try:
            lance = chess.Move.from_uci(uci)
        except ValueError:
            return False
        if lance not in board.legal_moves:
            return False
        board.push(lance)
        sala["oferta_empate"] = None
        _verificar_fim(sala)
        _tocar(sala)
        return True


def acao_abandonar(codigo, token):
    with LOJA["lock"]:
        sala = LOJA["salas"].get(codigo)
        if sala is None or sala["resultado"]:
            return
        cor = _papel(sala, token)
        if cor is None:
            return
        placar = "0-1" if cor == chess.WHITE else "1-0"
        sala["resultado"] = (placar, f"{NOME_COR[cor]} abandonaram")
        _tocar(sala)


def acao_oferecer_empate(codigo, token):
    with LOJA["lock"]:
        sala = LOJA["salas"].get(codigo)
        if sala is None or sala["resultado"]:
            return
        cor = _papel(sala, token)
        if cor is None or sala["oferta_empate"] is not None:
            return
        sala["oferta_empate"] = cor
        _tocar(sala)


def acao_responder_empate(codigo, token, aceitar):
    with LOJA["lock"]:
        sala = LOJA["salas"].get(codigo)
        if sala is None or sala["resultado"]:
            return
        cor = _papel(sala, token)
        if cor is None or sala["oferta_empate"] in (None, cor):
            return
        if aceitar:
            sala["resultado"] = ("1/2-1/2", "Empate por acordo")
        sala["oferta_empate"] = None
        _tocar(sala)


def acao_revanche(codigo, token):
    """Nova partida com as cores trocadas (só depois que a anterior acabou)."""
    with LOJA["lock"]:
        sala = LOJA["salas"].get(codigo)
        if sala is None or sala["resultado"] is None:
            return
        if _papel(sala, token) is None:
            return
        j = sala["jogadores"]
        j[chess.WHITE], j[chess.BLACK] = j[chess.BLACK], j[chess.WHITE]
        sala["board"] = chess.Board()
        sala["resultado"] = None
        sala["oferta_empate"] = None
        sala["partidas"] += 1
        _tocar(sala)


# ----------------------------------------------------------------------------
# Visual
# ----------------------------------------------------------------------------
# Imagens das 12 peças (SVG do python-chess), embutidas no componente do tabuleiro.
SVGS = {
    chess.Piece(tipo, cor_peca).symbol(): chess.svg.piece(chess.Piece(tipo, cor_peca))
    for tipo in chess.PIECE_TYPES
    for cor_peca in (chess.WHITE, chess.BLACK)
}

# Tabuleiro clicável: um mini-site (HTML + JavaScript) que roda dentro de um iframe
# e conversa com o Streamlit. O Python manda a posição e os lances legais; o JavaScript
# cuida de selecionar a peça, mostrar as bolinhas e devolver o lance escolhido.
HTML_TABULEIRO = r"""<!doctype html>
<html lang="pt-BR" translate="no">
<head>
<meta charset="utf-8">
<meta name="google" content="notranslate">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body { margin: 0; padding: 0; background: transparent; }
  #wrap { position: relative; width: 100%; max-width: 520px; margin: 0 auto;
          -webkit-user-select: none; user-select: none; -webkit-tap-highlight-color: transparent; }
  #board { display: grid; grid-template-columns: repeat(8, 1fr); width: 100%;
           border-radius: 6px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,.4); }
  .sq { position: relative; aspect-ratio: 1 / 1; }
  .claro { background: #f0d9b5; }
  .escuro { background: #b58863; }
  .sq.mexivel { cursor: pointer; }
  .sq::before { content: ""; position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 0; }
  .sq.ultimo::before { background: rgba(255, 235, 59, .45); }
  .sq.sel::before { background: rgba(60, 160, 80, .6); }
  .sq.xeque::before { background: radial-gradient(circle, rgba(255,0,0,.9) 0%, rgba(255,0,0,.45) 55%, rgba(255,0,0,0) 78%); }
  .peca { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 1; pointer-events: none; }
  .dest::after { content: ""; position: absolute; z-index: 2; left: 50%; top: 50%; width: 32%; height: 32%;
                 transform: translate(-50%, -50%); border-radius: 50%; background: rgba(20, 80, 30, .55); pointer-events: none; }
  .dest.cap::after { width: 90%; height: 90%; background: transparent; border: 5px solid rgba(20, 80, 30, .6); box-sizing: border-box; }
  .coord { position: absolute; z-index: 3; font: 600 10px/1 sans-serif; pointer-events: none; }
  .coord.num { top: 2px; left: 3px; }
  .coord.let { bottom: 2px; right: 3px; }
  .claro .coord { color: #b58863; }
  .escuro .coord { color: #f0d9b5; }
  #promo { display: none; position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 10;
           background: rgba(0,0,0,.55); align-items: center; justify-content: center; border-radius: 6px; }
  #promo.aberto { display: flex; }
  #opcoes { display: flex; gap: 8px; background: #fff; padding: 10px; border-radius: 10px; }
  #opcoes button { width: 18vw; max-width: 80px; aspect-ratio: 1 / 1; border: 2px solid #999; border-radius: 8px;
                   background: #f0d9b5; padding: 0; cursor: pointer; }
  #opcoes button img { width: 100%; height: 100%; display: block; }
</style>
</head>
<body>
<div id="wrap">
  <div id="board"></div>
  <div id="promo"><div id="opcoes"></div></div>
</div>
<script>
const SVGS = __SVGS__;
const URIS = {};
for (const k in SVGS) URIS[k] = "data:image/svg+xml;utf8," + encodeURIComponent(SVGS[k]);
const LETRAS = "abcdefgh";
let args = null, selecionada = null, promo = null, bloqueado = false, timer = null;

function enviar(m) {
  window.parent.postMessage(Object.assign({ isStreamlitMessage: true }, m), "*");
}
function ajustarAltura() {
  enviar({ type: "streamlit:setFrameHeight", height: document.getElementById("wrap").offsetHeight + 4 });
}

function desenhar() {
  const tab = document.getElementById("board");
  tab.innerHTML = "";
  const branco = args.orientacao !== "black";
  const legal = args.legal || {};
  const destinos = selecionada ? (legal[selecionada] || []) : [];
  for (let r = 0; r < 8; r++) {
    for (let c = 0; c < 8; c++) {
      const arq = branco ? c : 7 - c;
      const fila = branco ? 7 - r : r;
      const nome = LETRAS[arq] + (fila + 1);
      const sq = document.createElement("div");
      let cls = "sq " + (((arq + fila) % 2 === 1) ? "claro" : "escuro");
      if (args.ultimo && (nome === args.ultimo[0] || nome === args.ultimo[1])) cls += " ultimo";
      if (nome === args.xeque) cls += " xeque";
      if (nome === selecionada) cls += " sel";
      const d = destinos.find(x => x.a === nome);
      if (d) cls += " dest" + (d.c ? " cap" : "");
      if (args.pode && !bloqueado && (legal[nome] || d)) cls += " mexivel";
      sq.className = cls;
      const p = args.pecas[nome];
      if (p) {
        const img = document.createElement("img");
        img.className = "peca";
        img.src = URIS[p];
        img.draggable = false;
        sq.appendChild(img);
      }
      if (c === 0) {
        const n = document.createElement("span");
        n.className = "coord num";
        n.textContent = String(fila + 1);
        sq.appendChild(n);
      }
      if (r === 7) {
        const l = document.createElement("span");
        l.className = "coord let";
        l.textContent = LETRAS[arq];
        sq.appendChild(l);
      }
      sq.addEventListener("click", () => clicar(nome));
      tab.appendChild(sq);
    }
  }
  ajustarAltura();
}

function clicar(nome) {
  if (!args || !args.pode || bloqueado || promo) return;
  const legal = args.legal || {};
  if (selecionada && selecionada !== nome) {
    const m = (legal[selecionada] || []).find(x => x.a === nome);
    if (m) {
      if (m.p) { promo = { de: selecionada, para: nome }; abrirPromo(); return; }
      mover(selecionada, nome, "");
      return;
    }
  }
  selecionada = (legal[nome] && selecionada !== nome) ? nome : null;
  desenhar();
}

function mover(de, para, promocao) {
  bloqueado = true;
  fecharPromo();
  desenhar();
  clearTimeout(timer);
  timer = setTimeout(() => { bloqueado = false; desenhar(); }, 3000);
  const id = Date.now() + "-" + Math.random().toString(36).slice(2);
  enviar({ type: "streamlit:setComponentValue", value: { id: id, de: de, para: para, promocao: promocao }, dataType: "json" });
}

function abrirPromo() {
  const op = document.getElementById("opcoes");
  op.innerHTML = "";
  const branco = args.cor !== "black";
  for (const t of ["q", "r", "b", "n"]) {
    const b = document.createElement("button");
    const img = document.createElement("img");
    img.src = URIS[branco ? t.toUpperCase() : t];
    b.appendChild(img);
    b.addEventListener("click", ev => {
      ev.stopPropagation();
      const p = promo;
      if (p) mover(p.de, p.para, t);
    });
    op.appendChild(b);
  }
  document.getElementById("promo").classList.add("aberto");
}
function fecharPromo() {
  promo = null;
  document.getElementById("promo").classList.remove("aberto");
}
document.getElementById("promo").addEventListener("click", fecharPromo);

window.addEventListener("message", ev => {
  const d = ev.data;
  if (!d || d.type !== "streamlit:render") return;
  const novo = d.args;
  if (!args || novo.fen !== args.fen) { selecionada = null; fecharPromo(); }
  bloqueado = false;
  clearTimeout(timer);
  args = novo;
  desenhar();
});
window.addEventListener("resize", ajustarAltura);
enviar({ type: "streamlit:componentReady", apiVersion: 1 });
</script>
</body>
</html>
"""


@st.cache_resource
def _declarar_componente():
    """Grava o HTML do tabuleiro numa pasta temporária e registra o componente."""
    pasta = Path(tempfile.gettempdir()) / "xadrez_online_tabuleiro_v1"
    pasta.mkdir(parents=True, exist_ok=True)
    html = HTML_TABULEIRO.replace("__SVGS__", json.dumps(SVGS))
    (pasta / "index.html").write_text(html, encoding="utf-8")
    return components.declare_component("tabuleiro_xadrez_v1", path=str(pasta))


def desenhar_tabuleiro(board, cor, minha_vez, key):
    """Mostra o tabuleiro clicável. Devolve o lance escolhido (dict) ou None."""
    componente = _declarar_componente()
    pecas = {chess.square_name(sq): p.symbol() for sq, p in board.piece_map().items()}

    legal = {}  # só preenchido quando é a vez do jogador: {"e2": [{"a": "e4", "c": 0, "p": 0}, ...]}
    if minha_vez:
        for m in board.legal_moves:
            de, para = chess.square_name(m.from_square), chess.square_name(m.to_square)
            destinos = legal.setdefault(de, [])
            if not any(d["a"] == para for d in destinos):
                destinos.append(
                    {"a": para, "c": int(board.is_capture(m)), "p": int(m.promotion is not None)}
                )

    ultimo = None
    if board.move_stack:
        m = board.peek()
        ultimo = [chess.square_name(m.from_square), chess.square_name(m.to_square)]
    xeque = chess.square_name(board.king(board.turn)) if board.is_check() else None
    lado = "black" if cor is not None and cor == chess.BLACK else "white"

    return componente(
        pecas=pecas,
        legal=legal,
        pode=bool(minha_vez),
        orientacao=lado,
        cor=lado,
        fen=board.fen(),
        ultimo=ultimo,
        xeque=xeque,
        key=key,
        default=None,
    )


def historico_san(board):
    tmp = chess.Board()
    linhas = []
    for i, lance in enumerate(board.move_stack):
        san = tmp.san(lance)
        if i % 2 == 0:
            linhas.append(f"{i // 2 + 1}. {san}")
        else:
            linhas[-1] += f" {san}"
        tmp.push(lance)
    return linhas


def texto_resultado(resultado, nomes):
    placar, motivo = resultado
    if placar == "1-0":
        quem = f"Vitória das Brancas ({nomes[chess.WHITE]})"
    elif placar == "0-1":
        quem = f"Vitória das Pretas ({nomes[chess.BLACK]})"
    else:
        quem = "Empate"
    return f"**{quem}** — {motivo}"


# ----------------------------------------------------------------------------
# Sessão (o que este navegador sabe sobre a sala em que está)
# ----------------------------------------------------------------------------
def definir_sessao(codigo, token):
    st.session_state["codigo"] = codigo
    st.session_state["token"] = token
    if token:  # guarda na URL para sobreviver ao F5
        st.query_params["sala"] = codigo
        st.query_params["p"] = token


def sair_da_sala():
    st.session_state.pop("codigo", None)
    st.session_state.pop("token", None)
    st.query_params.clear()


@st.fragment(run_every=INTERVALO)
def vigia(codigo, versao_vista):
    """Fragmento invisível: se a sala mudou, redesenha a página inteira."""
    with LOJA["lock"]:
        sala = LOJA["salas"].get(codigo)
        mudou = sala is None or sala["versao"] != versao_vista
    if mudou:
        st.rerun()


# ----------------------------------------------------------------------------
# Telas
# ----------------------------------------------------------------------------
def tela_inicial():
    st.title("♟️ Xadrez Online")
    st.caption("Crie uma sala, passe o código para alguém e joguem de computadores diferentes.")

    nome = st.text_input("Seu nome", max_chars=20, placeholder="Como quer ser chamado?")
    nome = nome.strip() or "Jogador"

    aba_criar, aba_entrar = st.tabs(["Criar sala", "Entrar numa sala"])

    with aba_criar:
        preferencia = st.radio("Jogar de", ["Aleatório", "Brancas", "Pretas"], horizontal=True)
        if st.button("Criar sala", type="primary"):
            codigo, token = criar_sala(nome, preferencia)
            definir_sessao(codigo, token)
            st.rerun()

    with aba_entrar:
        padrao = st.query_params.get("sala", "")
        codigo = st.text_input("Código da sala", value=padrao, max_chars=5, placeholder="Ex.: K7QX2")
        codigo = codigo.strip().upper()
        if st.button("Entrar", type="primary"):
            status, token = entrar_sala(codigo, nome)
            if status == "inexistente":
                st.error("Não encontrei essa sala. Confira o código.")
            else:
                if status == "cheia":
                    st.toast("Sala cheia: você entrou como espectador.")
                definir_sessao(codigo, token)
                st.rerun()


def tela_jogo():
    codigo = st.session_state["codigo"]
    token = st.session_state.get("token")
    snap = snapshot(codigo, token)

    st.title("♟️ Xadrez Online")

    if snap is None:
        st.error("Essa sala não existe mais (ela expirou ou o servidor reiniciou).")
        if st.button("Voltar ao início"):
            sair_da_sala()
            st.rerun()
        return

    vigia(codigo, snap["versao"])

    board, nomes, cor = snap["board"], snap["nomes"], snap["cor"]
    resultado, oferta = snap["resultado"], snap["oferta"]
    aguardando = nomes[chess.WHITE] is None or nomes[chess.BLACK] is None
    em_andamento = resultado is None and not aguardando
    minha_vez = em_andamento and cor is not None and board.turn == cor

    # --- cabeçalho ---
    st.markdown(f"**Código da sala:** `{codigo}`")
    for c in (chess.WHITE, chess.BLACK):
        nome = nomes[c] or "_aguardando jogador…_"
        voce = " (você)" if c == cor else ""
        st.markdown(f"{ICONE_COR[c]} {NOME_COR[c]}: {nome}{voce}")
    if cor is None:
        st.caption("Você está assistindo como espectador.")

    # --- status ---
    if resultado:
        st.success(texto_resultado(resultado, nomes))
    elif aguardando:
        st.info(f"Aguardando o adversário. Passe o código **{codigo}** para ele.")
    else:
        msg = "Sua vez!" if minha_vez else f"Vez de {nomes[board.turn]} ({NOME_COR[board.turn]})."
        if board.is_check():
            msg += " ⚠️ Xeque!"
        st.info(msg)

    # --- tabuleiro clicável: toque na peça e depois na casa de destino ---
    resposta = desenhar_tabuleiro(board, cor, minha_vez, key=f"tab_{codigo}_{snap['partidas']}")
    if resposta and resposta.get("id") != st.session_state.get("ultimo_lance_id"):
        # o Streamlit "lembra" do último valor do componente, então confere se é um lance novo
        st.session_state["ultimo_lance_id"] = resposta.get("id")
        if minha_vez:
            acao_jogar(codigo, token, resposta["de"] + resposta["para"] + (resposta.get("promocao") or ""))
            st.rerun()
    if minha_vez:
        st.caption("Toque numa peça sua e depois na casa para onde quer ir.")

    # --- empate / abandono / revanche ---
    if cor is not None and em_andamento:
        if oferta is not None and oferta != cor:
            st.warning("O adversário ofereceu empate.")
            a, b = st.columns(2)
            if a.button("Aceitar empate", use_container_width=True):
                acao_responder_empate(codigo, token, True)
                st.rerun()
            if b.button("Recusar", use_container_width=True):
                acao_responder_empate(codigo, token, False)
                st.rerun()
        else:
            a, b = st.columns(2)
            if oferta == cor:
                a.button("Empate oferecido…", disabled=True, use_container_width=True)
            elif a.button("Oferecer empate", use_container_width=True):
                acao_oferecer_empate(codigo, token)
                st.rerun()
            if b.button("Abandonar", use_container_width=True):
                acao_abandonar(codigo, token)
                st.rerun()

    if cor is not None and resultado:
        if st.button("Nova partida (trocar de cor)", type="primary"):
            acao_revanche(codigo, token)
            st.rerun()

    # --- histórico ---
    with st.expander("Lances da partida", expanded=False):
        linhas = historico_san(board)
        st.write("  \n".join(linhas) if linhas else "Nenhum lance ainda.")

    st.divider()
    if st.button("Sair da sala"):
        sair_da_sala()
        st.rerun()


# ----------------------------------------------------------------------------
# Início: recupera a sessão se a pessoa deu F5 (token guardado na URL)
# ----------------------------------------------------------------------------
if "codigo" not in st.session_state:
    sala_url = st.query_params.get("sala", "").upper()
    token_url = st.query_params.get("p")
    if sala_url and token_url:
        st.session_state["codigo"] = sala_url
        st.session_state["token"] = token_url

if "codigo" in st.session_state:
    tela_jogo()
else:
    tela_inicial()
