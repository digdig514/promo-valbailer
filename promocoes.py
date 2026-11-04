#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
promocoes.py — compara as promoções da semana (circulaires) do
Maxi, Metro, IGA e Super C para a sua lista de compras.

COMO USAR
    Windows:      py promocoes.py
    Mac / Linux:  python3 promocoes.py

    O relatório abre sozinho no navegador (relatorio_promocoes.html).

OPÇÕES
    --cp G3K2S5       seu código postal (os folhetos variam por região)
    --lista ARQUIVO   outra lista de compras (padrão: lista_compras.txt)
    --csv             também salva uma planilha .csv com todas as ofertas
    --sem-cache       ignora o cache (que dura 6 horas) e busca tudo de novo
    --nao-abrir       não abre o navegador no final

FONTE DOS DADOS
    A mesma busca pública de folhetos usada pelo site/app Flipp.
    Não é uma API oficial: se um dia mudar e parar de funcionar, o script avisa.
    Usa só a biblioteca padrão do Python 3.8+ (não precisa instalar nada).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

# =====================================================================
#  CONFIGURAÇÃO — pode mexer aqui
# =====================================================================

CODIGO_POSTAL = "G3K 2S5"  # troque pelo seu código postal

# Lojas comparadas. "padrao" identifica o nome da rede nos folhetos.
# Para incluir outra rede, copie uma linha. Ex.:
#   "Provigo": {"padrao": r"^provigo\b", "cor": "#7c3aed", "endereco": "..."},
LOJAS = {
    "Maxi":    {"padrao": r"^maxi\b",      "cor": "#c2410c",
                "endereco": "1065, boul. Pie-XI Nord, Val-Bélair"},
    "Metro":   {"padrao": r"^metro\b",     "cor": "#1d4ed8",
                "endereco": "Metro Plus — 1370, boul. Pie-XI Nord, Val-Bélair"},
    "IGA":     {"padrao": r"^iga\b",       "cor": "#b91c1c",
                "endereco": "IGA Extra — 1819, av. Industrielle, Val-Bélair"},
    "Super C": {"padrao": r"^super\s?c\b", "cor": "#15803d",
                "endereco": "4545, boul. de l'Auvergne, Neufchâtel (o mais perto, ~6 km)"},
}

# Categorias da seção "Destaques da semana" (palavras em francês, sem acento).
DESTAQUES = {
    "Frutas": {
        "inclui": "pomme banane fraise raisin orange clementine mandarine mangue ananas "
                  "poire bleuet framboise mure avocat kiwi melon cantaloup peche prune "
                  "nectarine cerise citron lime pamplemousse grenade papaye",
        "exclui": "terre",
    },
    "Legumes e verduras": {
        "inclui": "tomate laitue brocoli carotte oignon poivron concombre champignon chou "
                  "celeri courgette epinard asperge haricot aubergine radis chou-fleur "
                  "patate courge",
        "exclui": "",
    },
    "Carnes": {
        "inclui": "boeuf porc poulet dinde agneau veau bifteck steak cotelette longe "
                  "saucisse jambon cuisse poitrine bacon surlonge faux-filet contre-filet "
                  "cotes",
        "exclui": "pizza lasagne pate nugget croquette pane burger galette hot-dog sandwich "
                  "bouillon soupe repas",
    },
    "Peixes e frutos do mar": {
        "inclui": "saumon crevette morue tilapia aiglefin truite petoncle homard crabe moule "
                  "sole fletan",
        "exclui": "sushi",
    },
}
# Palavras que tiram um produto de TODOS os destaques (produtos processados etc.)
EXCLUIR_SEMPRE = (
    "jus yogourt yaourt croustille chips saveur confiture compote soupe bouillon sauce "
    "chat chien barre biscuit cereale gateau tarte boisson bonbon chocolat glace muffin "
    "pain vinaigrette assaisonnement epice nourriture litiere savon shampooing tisane the "
    "gelee sorbet bouchee collation"
)
DESTAQUES_POR_CATEGORIA = 12

CACHE_HORAS = 6
PAUSA_ENTRE_BUSCAS = 0.6  # segundos (para não sobrecarregar o serviço)

# =====================================================================

API = "https://backflipp.wishabi.com/flipp/items/search"
PASTA = Path(__file__).resolve().parent
LB_POR_KG = 2.20462
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
STOPWORDS = {"de", "du", "des", "la", "le", "les", "d", "l", "a", "au", "aux", "en", "et", "ou"}


# ----------------------------------------------------------------- texto
def norm(s: str | None) -> str:
    """minúsculas, sem acentos, œ -> oe, espaços simples."""
    s = (s or "").lower().replace("œ", "oe").replace("æ", "ae").replace("’", "'")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


def rx_palavra(palavra: str) -> re.Pattern:
    """Casa a palavra inteira, aceitando plural/feminino (fraise ~ fraises, hache ~ hachee)."""
    w = norm(palavra)
    base = w[:-1] if len(w) > 4 and w[-1] in "sx" else w
    return re.compile(r"\b" + re.escape(base) + r"(?:e|s|x|es)?\b")


def rx_prefixo(palavra: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(norm(palavra)))


def palavras(texto: str) -> list[str]:
    return [w for w in re.split(r"[\s']+", norm(texto)) if w and w not in STOPWORDS]


def para_float(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def dinheiro(v: float | None) -> str:
    return "—" if v is None else f"{v:,.2f} $".replace(",", " ").replace(".", ",")


def dia(d: dt.datetime | dt.date | None) -> str:
    return "?" if d is None else f"{DIAS[d.weekday()]} {d:%d/%m}"


def data_iso(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


# ----------------------------------------------------------------- busca
class ErroFonte(Exception):
    pass


def buscar_api(termo: str, cp: str, usar_cache: bool = True) -> dict:
    """Busca um termo nos folhetos da região do código postal."""
    params = {"locale": "fr-ca", "postal_code": cp, "q": termo}
    url = API + "?" + urllib.parse.urlencode(params)
    cache_dir = PASTA / ".cache_promocoes"
    arq = cache_dir / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")
    if usar_cache and arq.exists() and time.time() - arq.stat().st_mtime < CACHE_HORAS * 3600:
        try:
            return json.loads(arq.read_text("utf-8"))
        except (OSError, ValueError):
            pass

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (comparador de promocoes pessoal)",
        "Accept": "application/json",
    })
    ultimo_erro = None
    for tentativa in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                dados = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise ErroFonte(
                    "Erro de certificado SSL. No Mac, abra a pasta Aplicativos > Python 3.x "
                    "e dê dois cliques em 'Install Certificates.command'; depois rode de novo."
                ) from e
            ultimo_erro = e
        except (TimeoutError, ValueError, OSError) as e:
            ultimo_erro = e
        time.sleep(1.5 * (tentativa + 1))
    else:
        raise ErroFonte(f"não consegui buscar '{termo}': {ultimo_erro}")

    cache_dir.mkdir(exist_ok=True)
    try:
        arq.write_text(json.dumps(dados), "utf-8")
    except OSError:
        pass
    time.sleep(PAUSA_ENTRE_BUSCAS)
    return dados


# ----------------------------------------------------------------- ofertas
@dataclass
class Oferta:
    id: str
    loja: str
    nome: str
    preco: float | None        # preço como aparece no folheto
    qtd: int                   # "2/5 $" -> 2
    unidade: str               # 'lb', 'kg', '100g' ou 'un'
    preco_un: float | None     # preço de 1 unidade (ou 1 lb / 1 kg)
    preco_kg: float | None
    kg_aprox: bool             # $/kg calculado pelo peso do pacote
    preco_original: float | None
    desconto: float | None     # 0.25 = 25 %
    texto_pre: str
    texto_pos: str
    historia: str
    inicio: dt.datetime | None
    fim: dt.datetime | None
    imagem: str
    nome_norm: str = ""

    def periodo(self, agora: dt.datetime) -> str:
        if self.fim and self.fim < agora:
            return "expirada"
        if self.inicio and self.inicio > agora:
            return "proxima"
        return "atual"

    def chave_preco(self):
        """Ordenação: primeiro quem tem $/kg (comparável), depois preço por unidade."""
        if self.preco_kg is not None:
            return (0, self.preco_kg)
        if self.preco_un is not None:
            return (1, self.preco_un)
        return (2, 0.0)

    def preco_txt(self) -> str:
        if self.preco is None:
            return self.historia or self.texto_pre or "ver folheto"
        s = dinheiro(self.preco)
        if self.qtd > 1:
            s = f"{self.qtd} / {s}"
        if self.unidade in ("lb", "kg"):
            s += f" /{self.unidade}"
        elif self.unidade == "100g":
            s += " /100 g"
        return s


def identificar_loja(merchant: str) -> str | None:
    m = norm(merchant)
    for loja, cfg in LOJAS.items():
        if re.search(cfg["padrao"], m):
            return loja
    return None


def unidade_de(texto_pos: str) -> str:
    t = norm(texto_pos)
    achados = []
    for rotulo, rx in (("100g", r"100\s?g\b"), ("kg", r"kg\b"), ("lb", r"\blbs?\b|livre")):
        m = re.search(rx, t)
        if m:
            achados.append((m.start(), rotulo))
    return min(achados)[1] if achados else "un"


def peso_do_nome(nome_norm: str) -> float | None:
    """Peso do pacote em kg a partir do nome (ex.: '454 g', '1,5 kg', '3 lb')."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|lb)\b", nome_norm)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    kg = {"kg": n, "g": n / 1000, "lb": n / LB_POR_KG}[m.group(2)]
    return kg if 0.05 <= kg <= 20 else None


def calc_desconto(preco, preco_un, qtd, original, historia) -> float | None:
    d = None
    if original and preco:
        if original > preco:
            d = 1 - preco / original
        elif qtd > 1 and preco_un and original > preco_un:
            d = 1 - preco_un / original
    if d is None:
        h = norm(historia)
        m = re.search(r"(\d{1,2})\s*%", h)
        if m:
            d = int(m.group(1)) / 100
        else:
            m = re.search(r"(?:economisez|epargnez|save|rabais)\D{0,8}(\d+(?:[.,]\d+)?)", h)
            if m and preco_un:
                valor = float(m.group(1).replace(",", "."))
                d = valor / (preco_un * max(qtd, 1) + valor)
    return d if d is not None and 0.01 <= d <= 0.9 else None


def montar_oferta(it: dict) -> Oferta | None:
    loja = identificar_loja(it.get("merchant_name") or it.get("merchant") or "")
    if not loja:
        return None
    nome = (it.get("name") or "").strip()
    if not nome:
        return None
    nome_n = norm(nome)
    pre = (it.get("pre_price_text") or "").strip()
    pos = (it.get("post_price_text") or "").strip()
    historia = (it.get("sale_story") or "").strip()
    preco = para_float(it.get("current_price"))

    qtd = 1
    m = re.search(r"(\d+)\s*(?:/|pour\b|for\b|x\b)", norm(pre))
    if m and 1 < int(m.group(1)) <= 12:
        qtd = int(m.group(1))
    unidade = unidade_de(pos)
    preco_un = preco / qtd if preco is not None else None

    preco_kg, aprox = None, False
    if preco_un is not None:
        if unidade == "lb":
            preco_kg = preco_un * LB_POR_KG
        elif unidade == "kg":
            preco_kg = preco_un
        elif unidade == "100g":
            preco_kg = preco_un * 10
        else:
            kg = peso_do_nome(nome_n)
            if kg:
                preco_kg, aprox = preco_un / kg, True

    original = para_float(it.get("original_price"))
    img = it.get("clean_image_url") or it.get("clipping_image_url") or ""
    return Oferta(
        id=str(it.get("id") or it.get("flyer_item_id") or hash((loja, nome, preco))),
        loja=loja, nome=nome, preco=preco, qtd=qtd, unidade=unidade,
        preco_un=preco_un, preco_kg=preco_kg, kg_aprox=aprox,
        preco_original=original,
        desconto=calc_desconto(preco, preco_un, qtd, original, historia),
        texto_pre=pre, texto_pos=pos, historia=historia,
        inicio=data_iso(it.get("valid_from")), fim=data_iso(it.get("valid_to")),
        imagem=img if str(img).startswith("https://") else "",
        nome_norm=nome_n,
    )


# ----------------------------------------------------------------- lista
@dataclass
class ItemLista:
    secao: str
    rotulo: str
    alternativas: list[str]
    excluir: list[str]

    def __post_init__(self):
        self._regs = [[rx_palavra(w) for w in palavras(a)] for a in self.alternativas]
        self._regs = [r for r in self._regs if r]
        self._excl = [rx_prefixo(w) for w in self.excluir]

    def casa(self, o: Oferta) -> bool:
        n = o.nome_norm
        if any(e.search(n) for e in self._excl):
            return False
        return any(all(r.search(n) for r in regs) for regs in self._regs)


def ler_lista(caminho: Path) -> list[ItemLista]:
    itens, secao = [], "Minha lista"
    for linha in caminho.read_text("utf-8").splitlines():
        l = linha.strip()
        if not l or l.startswith("#"):
            continue
        if l.startswith("[") and l.endswith("]"):
            secao = l[1:-1].strip() or secao
            continue
        rotulo, resto = (l.split("=", 1) if "=" in l else (l, l))
        tokens = resto.split()
        excluir = [t[1:] for t in tokens if t.startswith("-") and len(t) > 1]
        texto = " ".join(t for t in tokens if not (t.startswith("-") and len(t) > 1))
        alternativas = [a.strip() for a in texto.split("|") if a.strip()]
        if alternativas:
            itens.append(ItemLista(secao, rotulo.strip(), alternativas, excluir))
    return itens


# ----------------------------------------------------------------- análise
def deduplicar(ofertas):
    vistos, saida = set(), []
    for o in ofertas:
        k = (o.loja, o.nome_norm, o.preco, o.inicio.date() if o.inicio else None)
        if k not in vistos:
            vistos.add(k)
            saida.append(o)
    return saida


def melhor(ofertas):
    """Ofertas com preço, ordenadas; devolve (lista ordenada, lojas empatadas no 1º lugar)."""
    com_preco = sorted([o for o in ofertas if o.preco is not None], key=Oferta.chave_preco)
    if not com_preco:
        return [], set()
    tipo, valor = com_preco[0].chave_preco()
    lojas = {o.loja for o in com_preco
             if o.chave_preco()[0] == tipo and o.chave_preco()[1] <= valor * 1.005}
    return com_preco, lojas


def analisar(lista, pool, agora):
    resultados = []
    for item in lista:
        achadas = deduplicar([o for o in pool if item.casa(o)])
        atual = [o for o in achadas if o.periodo(agora) == "atual"]
        prox = [o for o in achadas if o.periodo(agora) == "proxima"]
        ord_atual, lojas_atual = melhor(atual)
        ord_prox, lojas_prox = melhor(prox)
        sem_preco = [o for o in achadas if o.preco is None]

        dica = ""
        if ord_atual and ord_prox:
            a, p = ord_atual[0].chave_preco(), ord_prox[0].chave_preco()
            if a[0] == p[0] and p[1] < a[1] * 0.97:
                o = ord_prox[0]
                dica = f"Espere: fica mais barato a partir de {dia(o.inicio)} no {o.loja}"
        elif ord_prox and not ord_atual:
            o = ord_prox[0]
            dica = f"Entra em promoção a partir de {dia(o.inicio)} no {o.loja}"

        resultados.append({
            "item": item,
            "atual": ord_atual, "lojas_atual": lojas_atual,
            "proxima": ord_prox, "lojas_proxima": lojas_prox,
            "sem_preco": [o for o in sem_preco if o.periodo(agora) != "expirada"],
            "dica": dica,
        })
    return resultados


def roteiro(resultados, periodo):
    """Para cada loja, quais itens da lista estão com o melhor preço nela."""
    por_loja = defaultdict(list)
    for r in resultados:
        ordenadas = r[periodo]
        for loja in sorted(r["lojas_" + periodo]):
            of = next(o for o in ordenadas if o.loja == loja)
            por_loja[loja].append((r["item"], of))
    return dict(sorted(por_loja.items(), key=lambda kv: -len(kv[1])))


def periodos_folhetos(pool, agora):
    """Datas de validade mais comuns de cada loja (atual e próximo folheto)."""
    cont = defaultdict(Counter)
    for o in pool:
        if o.inicio and o.fim:
            per = o.periodo(agora)
            if per != "expirada":
                cont[(o.loja, per)][(o.inicio.date(), o.fim.date())] += 1
    saida = {}
    for loja in LOJAS:
        saida[loja] = {per: (cont[(loja, per)].most_common(1)[0][0] if cont[(loja, per)] else None)
                       for per in ("atual", "proxima")}
    return saida


def destaques(pool, agora):
    excl_geral = [rx_prefixo(w) for w in EXCLUIR_SEMPRE.split()]
    saida = {}
    for cat, cfg in DESTAQUES.items():
        inc = [rx_palavra(w) for w in cfg["inclui"].split()]
        exc = excl_geral + [rx_prefixo(w) for w in cfg["exclui"].split()]
        sel = [o for o in pool
               if o.preco is not None and o.periodo(agora) != "expirada"
               and any(r.search(o.nome_norm) for r in inc)
               and not any(e.search(o.nome_norm) for e in exc)]
        sel = deduplicar(sel)
        # quem tem desconto conhecido primeiro (maior desconto), depois menor $/kg
        sel.sort(key=lambda o: (o.desconto is None, -(o.desconto or 0), o.chave_preco()))
        saida[cat] = sel[:DESTAQUES_POR_CATEGORIA]
    return saida


# ----------------------------------------------------------------- relatório HTML
CSS = """
:root{--bg:#f6f5f2;--card:#fff;--txt:#1f2328;--mut:#667085;--lin:#e7e5e0;--ok:#ecfdf3;--okb:#12b76a;--warn:#fffaeb;--warnb:#f79009}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--txt)}
main{max-width:1100px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:34px 0 12px}h3{font-size:16px;margin:0}
.mut{color:var(--mut)}.peq{font-size:12.5px}
.card{background:var(--card);border:1px solid var(--lin);border-radius:12px;padding:16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.loja{display:inline-block;padding:1px 8px;border-radius:999px;color:#fff;font-size:12.5px;font-weight:600;white-space:nowrap}
.tag{display:inline-block;padding:1px 7px;border-radius:6px;background:#f2f4f7;font-size:12px;margin-left:4px}
.desc{background:#fef3f2;color:#b42318;font-weight:600}
.dica{background:var(--warn);border-left:4px solid var(--warnb);padding:8px 10px;border-radius:6px;margin-top:10px;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:14px;table-layout:fixed}
th.img{width:66px}th.c-loja{width:92px}th.c-preco{width:130px}th.c-kg{width:100px}th.c-val{width:170px}
th{font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--mut);text-align:left;font-weight:600;padding:6px;border-bottom:1px solid var(--lin)}
td{padding:7px 6px;border-bottom:1px solid var(--lin);vertical-align:middle}
tr.top td{background:var(--ok)}tr.top td:first-child{box-shadow:inset 3px 0 0 var(--okb)}
td.num{white-space:nowrap;font-variant-numeric:tabular-nums}
img.th{width:54px;height:54px;object-fit:contain;border-radius:6px;background:#fafafa;border:1px solid var(--lin)}
.sec{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:26px 0 4px}
.cab{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
.vazio{color:var(--mut);font-style:italic}
ul.rot{margin:6px 0 0;padding-left:18px}ul.rot li{margin:2px 0}
details summary{cursor:pointer;color:var(--mut);font-size:13.5px;margin-top:8px}
.scroll{overflow-x:auto}
@media (max-width:640px){td.img,th.img{display:none}h1{font-size:22px}table{table-layout:auto}}
"""


CABECALHO = ('<tr><th class="img"></th><th class="c-loja">Loja</th><th>Produto</th>'
             '<th class="c-preco">Preço</th><th class="c-kg">$/kg</th><th class="c-val">Validade</th></tr>')


def e(s) -> str:
    return html.escape(str(s or ""))


def badge(loja: str) -> str:
    return f'<span class="loja" style="background:{LOJAS[loja]["cor"]}">{e(loja)}</span>'


def preco_kg_txt(o: Oferta) -> str:
    if o.preco_kg is None:
        return "—"
    return ("≈ " if o.kg_aprox else "") + dinheiro(o.preco_kg)


def obs_txt(o: Oferta) -> str:
    partes = []
    if o.qtd > 1 and o.preco_un is not None:
        partes.append(f"= {dinheiro(o.preco_un)} cada")
    if o.texto_pre and o.qtd == 1:
        partes.append(o.texto_pre)
    if o.historia:
        partes.append(o.historia)
    if o.texto_pos and o.unidade == "un":
        partes.append(o.texto_pos)
    return " · ".join(partes)


def linha_oferta(o: Oferta, destaque: bool, mostrar_validade: bool = True) -> str:
    img = f'<img class="th" loading="lazy" src="{e(o.imagem)}" alt="">' if o.imagem else ""
    desc = f'<span class="tag desc">-{round(o.desconto * 100)}%</span>' if o.desconto else ""
    obs = obs_txt(o)
    obs_html = '<div class="mut peq">' + e(obs) + '</div>' if obs else ""
    val = f'<td class="num peq">{dia(o.inicio)} → {dia(o.fim)}</td>' if mostrar_validade else ""
    classe = "top" if destaque else ""
    return (
        f'<tr class="{classe}">'
        f'<td class="img">{img}</td><td>{badge(o.loja)}</td>'
        f'<td>{e(o.nome)}{desc}{obs_html}</td>'
        f'<td class="num"><b>{e(o.preco_txt())}</b></td>'
        f'<td class="num">{preco_kg_txt(o)}</td>{val}</tr>'
    )


def tabela(ofertas, lojas_top, limite=8) -> str:
    cab = CABECALHO
    linhas = []
    for i, o in enumerate(ofertas[:limite]):
        top = o.loja in lojas_top and o.chave_preco() <= (ofertas[0].chave_preco()[0],
                                                          ofertas[0].chave_preco()[1] * 1.005)
        linhas.append(linha_oferta(o, top))
    extra = ""
    if len(ofertas) > limite:
        resto = "".join(linha_oferta(o, False) for o in ofertas[limite:])
        extra = (f'<details><summary>+ {len(ofertas) - limite} outras ofertas</summary>'
                 f'<div class="scroll"><table>{cab}{resto}</table></div></details>')
    return f'<div class="scroll"><table>{cab}{"".join(linhas)}</table></div>{extra}'


def bloco_roteiro(titulo, rot, total_itens, subtitulo="") -> str:
    if not rot:
        return (f'<div class="card"><h3>{e(titulo)}</h3>'
                f'<p class="vazio">Nenhum item da sua lista em promoção neste período.</p></div>')
    lojas = list(rot)
    rec = f"<p>Se for a <b>uma loja só</b>: {badge(lojas[0])} — melhor preço em <b>{len(rot[lojas[0]])}</b> " \
          f"dos {total_itens} itens da lista."
    if len(lojas) > 1:
        cobertos = len({it.rotulo for l in lojas[:2] for it, _ in rot[l]})
        rec += f"<br>Combinando {badge(lojas[0])} + {badge(lojas[1])}: <b>{cobertos}</b> itens no melhor preço."
    rec += "</p>"
    cards = []
    for loja, itens in rot.items():
        lis = []
        for it, o in itens:
            kg = ""
            if o.preco_kg and o.unidade == "un":
                kg = ' <span class="mut">(' + preco_kg_txt(o) + "/kg)</span>"
            lis.append(f"<li>{e(it.rotulo)} — <b>{e(o.preco_txt())}</b>{kg}</li>")
        palavra = "item" if len(itens) == 1 else "itens"
        cards.append(f'<div class="card" style="margin:0">{badge(loja)} '
                     f'<b>{len(itens)} {palavra}</b>'
                     f'<div class="mut peq">{e(LOJAS[loja]["endereco"])}</div>'
                     f'<ul class="rot">{"".join(lis)}</ul></div>')
    sub = '<div class="mut peq">' + e(subtitulo) + "</div>" if subtitulo else ""
    return (f'<div class="card"><h3>{e(titulo)}</h3>{sub}'
            f'{rec}<div class="grid">{"".join(cards)}</div></div>')


def fmt_periodo(p) -> str:
    return "—" if not p else f"{dia(p[0])} → {dia(p[1])}"


def gerar_html(cp, agora, resultados, pool, periodos, dest, falhas) -> str:
    total = len(resultados)
    tem_proxima = any(r["proxima"] for r in resultados) or any(p["proxima"] for p in periodos.values())

    # ---- datas dos folhetos
    linhas_per = "".join(
        f'<tr><td>{badge(l)}</td><td class="num">{fmt_periodo(p["atual"])}</td>'
        f'<td class="num">{fmt_periodo(p["proxima"]) if p["proxima"] else "<span class=mut>ainda não publicado</span>"}</td>'
        f'<td class="mut peq">{e(LOJAS[l]["endereco"])}</td></tr>'
        for l, p in periodos.items())
    bloco_datas = (f'<div class="card"><h3>Validade dos folhetos</h3>'
                   f'<div class="scroll"><table><tr><th>Loja</th><th>Folheto atual</th>'
                   f'<th>Próximo folheto</th><th>Loja mais perto</th></tr>{linhas_per}</table></div>'
                   f'<p class="mut peq">No Québec os folhetos costumam trocar na quinta-feira. '
                   f'Quando o próximo já foi publicado, o relatório compara as duas semanas.</p></div>')

    # ---- onde comprar
    fim_atual = max((p["atual"][1] for p in periodos.values() if p["atual"]), default=None)
    ini_prox = min((p["proxima"][0] for p in periodos.values() if p["proxima"]), default=None)
    blocos = bloco_roteiro("Esta semana" + (f" (até {dia(fim_atual)})" if fim_atual else ""),
                           roteiro(resultados, "atual"), total)
    if tem_proxima:
        blocos += bloco_roteiro("Próxima semana" + (f" (a partir de {dia(ini_prox)})" if ini_prox else ""),
                                roteiro(resultados, "proxima"), total,
                                "Folhetos já publicados para a semana que vem.")

    dicas = [r for r in resultados if r["dica"]]
    bloco_dicas = ""
    if dicas:
        lis = "".join(f'<li><b>{e(r["item"].rotulo)}</b>: {e(r["dica"])}</li>' for r in dicas)
        bloco_dicas = f'<div class="card"><h3>Quando comprar</h3><ul class="rot">{lis}</ul></div>'

    sem_promo = [r["item"].rotulo for r in resultados if not r["atual"] and not r["proxima"]]

    # ---- detalhe por item
    det, secao = [], None
    for r in resultados:
        it = r["item"]
        if it.secao != secao:
            secao = it.secao
            det.append(f'<div class="sec">{e(secao)}</div>')
        busca = " | ".join(it.alternativas)
        corpo = ""
        if r["atual"]:
            corpo += tabela(r["atual"], r["lojas_atual"])
        if r["proxima"]:
            corpo += (f'<div class="mut peq" style="margin:12px 0 4px"><b>Próxima semana</b></div>'
                      + tabela(r["proxima"], r["lojas_proxima"], limite=5))
        if not corpo:
            corpo = '<p class="vazio">Sem promoção nos folhetos dessas lojas.</p>'
        if r["sem_preco"]:
            corpo += ('<details><summary>Ofertas sem preço fixo (ex.: % de desconto)</summary><ul class="rot">'
                      + "".join(f'<li>{badge(o.loja)} {e(o.nome)} — {e(o.historia or o.texto_pre)}</li>'
                                for o in r["sem_preco"][:8]) + "</ul></details>")
        if r["dica"]:
            corpo += f'<div class="dica">{e(r["dica"])}</div>'
        venc = (" ".join(badge(l) for l in sorted(r["lojas_atual"]))
                if r["lojas_atual"] else "")
        det.append(f'<div class="card"><div class="cab"><h3>{e(it.rotulo)} {venc}</h3>'
                   f'<span class="mut peq">busca: {e(busca)}</span></div>{corpo}</div>')

    # ---- destaques
    dest_html = ""
    for cat, ofs in dest.items():
        if not ofs:
            continue
        linhas = "".join(linha_oferta(o, False) for o in ofs)
        dest_html += (f'<div class="card"><h3>{e(cat)}</h3><div class="scroll"><table>'
                      f'{CABECALHO}{linhas}</table></div></div>')

    aviso = ""
    if falhas:
        aviso = (f'<div class="dica">Algumas buscas falharam ({len(falhas)}): '
                 f'{e(", ".join(falhas[:6]))}. Rode de novo daqui a pouco.</div>')
    lojas_sem_dados = [l for l in LOJAS if not any(o.loja == l for o in pool)]
    if lojas_sem_dados:
        aviso += (f'<div class="dica">Nenhuma oferta encontrada para: {e(", ".join(lojas_sem_dados))}. '
                  f'Confira o código postal ou o nome da loja em LOJAS no script.</div>')

    badges = " ".join(badge(l) for l in LOJAS)
    txt_sem_promo = ""
    if sem_promo:
        txt_sem_promo = ('<p class="mut peq">Sem promoção nos folhetos: '
                         + e(", ".join(sem_promo)) + ".</p>")
    secao_dest = ""
    if dest_html:
        secao_dest = ('<h2>Destaques da semana</h2><p class="mut peq">'
                      "Maiores descontos encontrados nos folhetos.</p>" + dest_html)
    detalhes = "".join(det)

    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Promoções da semana — {e(cp)}</title><style>{CSS}</style></head>
<body><main>
<h1>Promoções da semana</h1>
<div class="mut">{badges} · código postal {e(cp)} ·
gerado {dia(agora)} às {agora:%H:%M}</div>
{aviso}
<h2>Onde comprar</h2>
{blocos}
{bloco_dicas}
{txt_sem_promo}
{bloco_datas}
<h2>Sua lista, item por item</h2>
<p class="mut peq">Verde = melhor preço. Carnes e frutas são comparadas em $/kg
(1 lb = 0,454 kg); "≈" quer dizer $/kg calculado pelo peso do pacote.
Confira sempre o folheto: alguns preços exigem cartão de fidelidade ou quantidade mínima.</p>
{detalhes}
{secao_dest}
<p class="mut peq" style="margin-top:40px">Dados: folhetos públicos (fonte do app Flipp).
Preços podem variar por loja; o folheto oficial de cada rede é a referência.</p>
</main></body></html>"""


# ----------------------------------------------------------------- main
def configurar_saida():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    configurar_saida()
    ap = argparse.ArgumentParser(description="Compara promoções do Maxi, Metro, IGA e Super C.")
    ap.add_argument("--cp", default=CODIGO_POSTAL, help="código postal (ex.: G3K2S5)")
    ap.add_argument("--lista", default=str(PASTA / "lista_compras.txt"))
    ap.add_argument("--saida", default=str(PASTA / "relatorio_promocoes.html"))
    ap.add_argument("--csv", action="store_true", help="salva também um .csv com as ofertas")
    ap.add_argument("--sem-cache", action="store_true")
    ap.add_argument("--sem-destaques", action="store_true")
    ap.add_argument("--nao-abrir", action="store_true")
    args = ap.parse_args(argv)

    cp = re.sub(r"\s+", "", args.cp).upper()
    if not re.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d", cp):
        print(f"Código postal inválido: {args.cp}  (exemplo: G3K2S5)")
        return 2

    caminho_lista = Path(args.lista)
    if not caminho_lista.exists():
        print(f"Não achei a lista de compras: {caminho_lista}")
        print("Coloque o arquivo lista_compras.txt na mesma pasta do script.")
        return 2
    lista = ler_lista(caminho_lista)
    if not lista:
        print("A lista de compras está vazia.")
        return 2

    termos = []
    for it in lista:
        for a in it.alternativas:
            if a not in termos:
                termos.append(a)
    if not args.sem_destaques:
        # buscar pelo nome da loja traz o folheto inteiro (usado nos destaques)
        termos += [l.lower() for l in LOJAS if l.lower() not in termos]

    print(f"Buscando promoções para {cp} — {len(termos)} buscas...")
    brutos, falhas = [], []
    for i, t in enumerate(termos, 1):
        print(f"  [{i:>2}/{len(termos)}] {t}", end="\r" if sys.stdout.isatty() else "\n", flush=True)
        try:
            dados = buscar_api(t, cp, usar_cache=not args.sem_cache)
        except ErroFonte as ex:
            if "SSL" in str(ex):
                print("\n" + str(ex))
                return 1
            falhas.append(t)
            continue
        brutos.extend(dados.get("items") or [])
    print(" " * 60, end="\r")

    if len(falhas) == len(termos):
        print("\nNão consegui acessar os folhetos (sem internet ou a fonte mudou).")
        return 1

    agora = dt.datetime.now().astimezone()
    pool, vistos = [], set()
    for it in brutos:
        o = montar_oferta(it)
        if o and o.id not in vistos and o.periodo(agora) != "expirada":
            vistos.add(o.id)
            pool.append(o)

    resultados = analisar(lista, pool, agora)
    periodos = periodos_folhetos(pool, agora)
    dest = {} if args.sem_destaques else destaques(pool, agora)

    saida = Path(args.saida)
    saida.write_text(gerar_html(cp, agora, resultados, pool, periodos, dest, falhas), "utf-8")

    if args.csv:
        arq_csv = saida.with_suffix(".csv")
        with arq_csv.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["item", "semana", "loja", "produto", "preco_folheto", "preco_kg",
                        "desconto_%", "inicio", "fim", "obs"])
            for r in resultados:
                for per in ("atual", "proxima"):
                    for o in r[per]:
                        w.writerow([r["item"].rotulo, per, o.loja, o.nome, o.preco_txt(),
                                    f"{o.preco_kg:.2f}".replace(".", ",") if o.preco_kg else "",
                                    round(o.desconto * 100) if o.desconto else "",
                                    o.inicio.date() if o.inicio else "",
                                    o.fim.date() if o.fim else "", obs_txt(o)])
        print(f"Planilha: {arq_csv}")

    # ---- resumo no terminal
    print(f"\nOfertas encontradas: {len(pool)} (Maxi/Metro/IGA/Super C)")
    for nome_per, per in (("ESTA SEMANA", "atual"), ("PRÓXIMA SEMANA", "proxima")):
        rot = roteiro(resultados, per)
        if not rot:
            continue
        print(f"\nONDE COMPRAR — {nome_per}")
        for loja, itens in rot.items():
            nomes = ", ".join(it.rotulo for it, _ in itens)
            palavra = "item " if len(itens) == 1 else "itens"
            print(f"  {loja:<8} {len(itens):>2} {palavra}: {nomes}")
    dicas = [r for r in resultados if r["dica"]]
    if dicas:
        print("\nQUANDO COMPRAR")
        for r in dicas:
            print(f"  {r['item'].rotulo}: {r['dica']}")
    if falhas:
        print(f"\nAtenção: {len(falhas)} buscas falharam: {', '.join(falhas)}")
    print(f"\nRelatório completo: {saida}")
    if not args.nao_abrir:
        try:
            webbrowser.open(saida.resolve().as_uri())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
