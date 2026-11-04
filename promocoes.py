#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
promocoes.py — coleta os folhetos (circulaires) da semana do Maxi, Metro, IGA e
Super C e gera o app "Aubaines Val-Bélair" (uma página HTML com busca e filtros).

COMO USAR
    Windows:      py promocoes.py
    Mac / Linux:  python3 promocoes.py

    Gera:  relatorio_promocoes.html  (o app — abre sozinho no navegador)
           dados.json                (todas as ofertas, para quem quiser usar)

OPÇÕES
    --cp G3K2S5       código postal (os folhetos variam por região)
    --lista ARQUIVO   cesta básica comparada na aba "Où magasiner"
    --saida ARQUIVO   onde salvar o app (no GitHub: site/index.html)
    --csv             também salva uma planilha .csv da cesta
    --sem-cache       ignora o cache (6 h) e busca tudo de novo
    --nao-abrir       não abre o navegador no final

ARQUIVOS NECESSÁRIOS NA MESMA PASTA
    promocoes.py, modelo_app.html, lista_compras.txt

FONTE DOS DADOS
    Os mesmos folhetos públicos usados pelo site/app Flipp (não é API oficial).
    Usa só a biblioteca padrão do Python 3.8+.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
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
#  CONFIGURAÇÃO
# =====================================================================

CODIGO_POSTAL = "G3K 2S5"

# Lojas comparadas. "padrao" identifica a rede pelo nome no folheto.
# Para incluir outra rede, copie uma linha. Ex.:
#   "Provigo": {"padrao": r"^provigo\b", "cor": "#6d28d9", "endereco": "..."},
LOJAS = {
    "Maxi":    {"padrao": r"^maxi\b",      "cor": "#b45309",
                "endereco": "1065, boul. Pie-XI Nord, Val-Bélair"},
    "Metro":   {"padrao": r"^metro\b",     "cor": "#0369a1",
                "endereco": "Metro Plus — 1370, boul. Pie-XI Nord, Val-Bélair"},
    "IGA":     {"padrao": r"^iga\b",       "cor": "#be123c",
                "endereco": "IGA Extra — 1819, av. Industrielle, Val-Bélair"},
    "Super C": {"padrao": r"^super\s?c\b", "cor": "#15803d",
                "endereco": "4545, boul. de l'Auvergne, Neufchâtel (~6 km)"},
}

# Buscas extras para completar preço por lb/kg e rabais dos produtos frescos.
BUSCAS_EXTRAS = (
    "poulet boeuf porc dinde agneau veau saumon crevettes poisson jambon saucisses bacon "
    "pommes bananes fraises raisins oranges clementines poires bleuets framboises melon "
    "tomates laitue brocoli carottes oignons poivrons concombres champignons patates "
    "lait fromage yogourt oeufs beurre pain cafe"
).split()

# Categorias (palavras em francês, sem acento). A ordem importa: a primeira que casar vence.
CATEGORIAS = [
    ("menage", "detergent lessive savon shampooing revitalisant dentifrice brosse deodorant couche "
               "couches lingette lingettes papier essuie-tout mouchoir mouchoirs nettoyant javellisant "
               "poubelle assouplissant vaisselle pile piles ampoule rasoir hygienique tampon lotion "
               "litiere chat chien pharmacie vitamine"),
    ("boissons", "jus boisson boissons eau gazeuse soda cola liqueur biere vin cidre kombucha "
                 "energisante limonade"),
    ("boulangerie", "pain pains bagel bagels croissant croissants muffin muffins tortilla tortillas "
                    "pita brioche baguette gateau beigne beignes tarte"),
    ("surgeles", "surgele surgeles surgelee surgelees congele congelee glacee frites pizza pizzas "
                 "pogo"),
    ("epicerie", "pate pates spaghetti macaroni nouille nouilles riz cereale cereales gruau farine "
                 "sucre sel huile vinaigre cafe tisane biscuit biscuits craquelin craquelins "
                 "croustille croustilles chips chocolat bonbon bonbons soupe bouillon conserve sauce "
                 "ketchup moutarde mayonnaise vinaigrette confiture miel arachide noix amandes "
                 "collation barre barres granola popcorn olive olives cornichon cornichons epice "
                 "epices assaisonnement levure tartinade sirop compote"),
    ("laitiers", "lait fromage fromages yogourt yogourts yaourt beurre creme margarine oeuf oeufs "
                 "kefir cottage ricotta mozzarella cheddar"),
    ("poissons", "saumon crevette crevettes morue tilapia aiglefin truite petoncle petoncles homard "
                 "crabe moule moules poisson poissons thon sole fletan goberge surimi"),
    ("viandes", "boeuf porc poulet dinde agneau veau bifteck biftecks steak hache cotelette "
                "cotelettes longe saucisse saucisses jambon roti ailes aile cuisse cuisses poitrine "
                "poitrines bacon surlonge filet cote cotes viande charcuterie salami pepperoni"),
    ("legumes", "tomate tomates laitue brocoli carotte carottes oignon oignons poivron poivrons "
                "concombre concombres champignon champignons chou celeri courgette courgettes "
                "epinard epinards asperge asperges haricot haricots aubergine radis patate patates "
                "terre courge ail salade chou-fleur betterave betteraves navet poireau poireaux "
                "legume legumes"),
    ("fruits", "pomme pommes banane bananes fraise fraises raisin raisins orange oranges clementine "
               "clementines mandarine mandarines mangue mangues ananas poire poires bleuet bleuets "
               "framboise framboises mure mures avocat avocats kiwi kiwis melon melons cantaloup "
               "peche peches prune prunes nectarine cerise cerises citron citrons lime limes "
               "pamplemousse grenade papaye fruit fruits"),
]

CACHE_HORAS = 6
PAUSA_ENTRE_BUSCAS = 0.6

# =====================================================================

API = "https://backflipp.wishabi.com/flipp"
PASTA = Path(__file__).resolve().parent
LB_POR_KG = 2.20462
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
STOPWORDS = {"de", "du", "des", "la", "le", "les", "d", "l", "a", "au", "aux", "en", "et", "ou"}


# ----------------------------------------------------------------- texto
def norm(s: str | None) -> str:
    s = (s or "").lower().replace("œ", "oe").replace("æ", "ae").replace("’", "'")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


def rx_palavra(palavra: str) -> re.Pattern:
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


def dia(d) -> str:
    return "?" if d is None else f"{DIAS[d.weekday()]} {d:%d/%m}"


def data_iso(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def nome_exibicao(nome: str) -> str:
    """'TOMATES ROMA | ROMA TOMATOES' -> 'Tomates roma'."""
    n = re.split(r"\s+\|\s+", nome.strip())[0].strip(" ,")
    letras = [c for c in n if c.isalpha()]
    if letras and sum(c.isupper() for c in letras) / len(letras) > 0.6:
        n = n.lower()
        n = n[:1].upper() + n[1:]
    return n


# ----------------------------------------------------------------- busca
class ErroFonte(Exception):
    pass


def buscar_json(url: str, usar_cache: bool = True) -> dict:
    cache_dir = PASTA / ".cache_promocoes"
    arq = cache_dir / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")
    if usar_cache and arq.exists() and time.time() - arq.stat().st_mtime < CACHE_HORAS * 3600:
        try:
            return json.loads(arq.read_text("utf-8"))
        except (OSError, ValueError):
            pass
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (comparateur de circulaires communautaire)",
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
                    "Erro de certificado SSL. No Mac, abra Aplicativos > Python 3.x e dê dois "
                    "cliques em 'Install Certificates.command'; depois rode de novo.") from e
            ultimo_erro = e
        except (TimeoutError, ValueError, OSError) as e:
            ultimo_erro = e
        time.sleep(1.5 * (tentativa + 1))
    else:
        raise ErroFonte(f"falha em {url}: {ultimo_erro}")
    cache_dir.mkdir(exist_ok=True)
    try:
        arq.write_text(json.dumps(dados), "utf-8")
    except OSError:
        pass
    time.sleep(PAUSA_ENTRE_BUSCAS)
    return dados


def buscar_api(termo: str, cp: str, usar_cache: bool = True) -> dict:
    q = urllib.parse.urlencode({"locale": "fr-ca", "postal_code": cp, "q": termo})
    return buscar_json(f"{API}/items/search?{q}", usar_cache)


def buscar_folhetos(cp: str, usar_cache: bool = True) -> list:
    q = urllib.parse.urlencode({"locale": "fr-ca", "postal_code": cp})
    dados = buscar_json(f"{API}/flyers?{q}", usar_cache)
    return dados.get("flyers", []) if isinstance(dados, dict) else (dados or [])


def buscar_itens_folheto(flyer_id, usar_cache: bool = True) -> list:
    q = urllib.parse.urlencode({"locale": "fr-ca"})
    dados = buscar_json(f"{API}/flyers/{int(flyer_id)}?{q}", usar_cache)
    return dados.get("items", []) if isinstance(dados, dict) else []


# ----------------------------------------------------------------- ofertas
_RX_CATEG = [(c, [rx_palavra(w) for w in ws.split()]) for c, ws in CATEGORIAS]


def categoria(nome_norm: str) -> str:
    for cat, regs in _RX_CATEG:
        if any(r.search(nome_norm) for r in regs):
            return cat
    return "autres"


@dataclass
class Oferta:
    id: str
    loja: str
    nome: str
    marca: str
    preco: float | None
    qtd: int
    unidade: str
    preco_un: float | None
    preco_kg: float | None
    kg_aprox: bool
    preco_original: float | None
    desconto: float | None
    texto_pre: str
    texto_pos: str
    historia: str
    inicio: dt.datetime | None
    fim: dt.datetime | None
    imagem: str
    nome_norm: str = ""
    categoria: str = "autres"

    def periodo(self, agora: dt.datetime) -> str:
        if self.fim and self.fim < agora:
            return "expirada"
        if self.inicio and self.inicio > agora:
            return "proxima"
        return "atual"

    def chave_preco(self):
        if self.preco_kg is not None:
            return (0, self.preco_kg)
        if self.preco_un is not None:
            return (1, self.preco_un)
        return (2, 0.0)

    def preco_txt(self) -> str:
        if self.preco is None:
            return self.historia or self.texto_pre or "voir circulaire"
        s = dinheiro(self.preco)
        if self.qtd > 1:
            s = f"{self.qtd} / {s}"
        if self.unidade in ("lb", "kg"):
            s += f" /{self.unidade}"
        elif self.unidade == "100g":
            s += " /100 g"
        return s

    def obs(self) -> str:
        partes = []
        if self.texto_pre and self.qtd == 1:
            partes.append(self.texto_pre)
        if self.historia:
            partes.append(self.historia)
        if self.texto_pos and self.unidade == "un" and \
                norm(self.texto_pos).strip("/. ") not in ("ch", "ea", "each", "chacun", "chaque", "un"):
            partes.append(self.texto_pos)
        return " · ".join(partes)


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
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|lb)\b", nome_norm)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    kg = {"kg": n, "g": n / 1000, "lb": n / LB_POR_KG}[m.group(2)]
    return kg if 0.05 <= kg <= 20 else None


def calc_desconto(preco, preco_un, qtd, original, historia, desconto_fonte=None) -> float | None:
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
    if d is None and desconto_fonte not in (None, "", 0):
        v = para_float(desconto_fonte)
        if v:
            d = v / 100 if v > 1 else v
    return d if d is not None and 0.01 <= d <= 0.9 else None


def montar_oferta(loja: str, it: dict) -> Oferta | None:
    """Aceita item da busca (current_price, pre/post_price_text...) ou do folheto (price, discount)."""
    nome = (it.get("name") or "").strip()
    if not nome:
        return None
    nome_n = norm(nome + " " + (it.get("brand") or ""))
    pre = (it.get("pre_price_text") or "").strip()
    pos = (it.get("post_price_text") or "").strip()
    historia = (it.get("sale_story") or "").strip()

    qtd = 1
    m = re.search(r"(\d+)\s*(?:/|pour\b|for\b|x\b)", norm(pre))
    if m and 1 < int(m.group(1)) <= 12:
        qtd = int(m.group(1))
    if "current_price" in it:
        preco = para_float(it.get("current_price"))
    else:
        bruto = str(it.get("price") or "")
        mq = re.match(r"\s*(\d+)\s*/\s*(\d+(?:[.,]\d+)?)", bruto)
        if mq and qtd == 1:
            qtd, preco = int(mq.group(1)), float(mq.group(2).replace(",", "."))
        else:
            preco = para_float(bruto)
    if preco is not None and preco <= 0:
        preco = None

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
            kg = peso_do_nome(norm(nome))
            if kg:
                preco_kg, aprox = preco_un / kg, True

    original = para_float(it.get("original_price"))
    img = (it.get("cutout_image_url") or it.get("clean_image_url")
           or it.get("clipping_image_url") or "")
    img = str(img).replace("http://", "https://", 1)
    return Oferta(
        id=str(it.get("id") or it.get("flyer_item_id") or abs(hash((loja, nome, preco)))),
        loja=loja, nome=nome, marca=(it.get("brand") or "").strip(),
        preco=preco, qtd=qtd, unidade=unidade,
        preco_un=preco_un, preco_kg=preco_kg, kg_aprox=aprox, preco_original=original,
        desconto=calc_desconto(preco, preco_un, qtd, original, historia, it.get("discount")),
        texto_pre=pre, texto_pos=pos, historia=historia,
        inicio=data_iso(it.get("valid_from")), fim=data_iso(it.get("valid_to")),
        imagem=img if img.startswith("https://") else "",
        nome_norm=nome_n, categoria=categoria(norm(nome)),
    )


CAMPOS_BUSCA = ("current_price", "pre_price_text", "post_price_text", "sale_story", "original_price")


def coletar(cp: str, termos: list[str], usar_cache: bool, agora: dt.datetime):
    """Folhetos completos + buscas (que trazem /lb, rabais etc.). Devolve (ofertas, falhas, nº folhetos)."""
    brutos: dict[str, tuple[str, dict]] = {}
    falhas: list[str] = []
    n_folhetos = 0

    # 1) folhetos inteiros
    try:
        folhetos = buscar_folhetos(cp, usar_cache)
    except ErroFonte as ex:
        if "SSL" in str(ex):
            raise
        folhetos = []
        falhas.append("liste des circulaires")
    for fl in folhetos:
        loja = identificar_loja(str(fl.get("merchant") or fl.get("merchant_name") or ""))
        fim = data_iso(fl.get("valid_to"))
        if not loja or (fim and fim < agora) or not fl.get("id"):
            continue
        print(f"  circulaire {loja}: {fl.get('name') or fl.get('id')}")
        try:
            itens = buscar_itens_folheto(fl["id"], usar_cache)
        except ErroFonte:
            falhas.append(f"circulaire {loja}")
            continue
        n_folhetos += 1
        for it in itens:
            it = dict(it)
            it.setdefault("valid_from", fl.get("valid_from"))
            it.setdefault("valid_to", fl.get("valid_to"))
            if it.get("id") is not None:
                brutos[str(it["id"])] = (loja, it)

    # 2) buscas: completam os itens do folheto e trazem os que faltarem
    for i, t in enumerate(termos, 1):
        print(f"  [{i:>2}/{len(termos)}] {t}", end="\r" if sys.stdout.isatty() else "\n", flush=True)
        try:
            dados = buscar_api(t, cp, usar_cache)
        except ErroFonte as ex:
            if "SSL" in str(ex):
                raise
            falhas.append(t)
            continue
        for it in dados.get("items") or []:
            loja = identificar_loja(it.get("merchant_name") or it.get("merchant") or "")
            if not loja:
                continue
            chave = str(it.get("flyer_item_id") or it.get("id"))
            if chave in brutos:
                base = dict(brutos[chave][1])
                for c in CAMPOS_BUSCA:
                    if it.get(c) not in (None, ""):
                        base[c] = it[c]
                if not base.get("cutout_image_url"):
                    base["clean_image_url"] = it.get("clean_image_url")
                brutos[chave] = (loja, base)
            else:
                brutos[chave] = (loja, it)
    print(" " * 60, end="\r")

    ofertas, vistos = [], set()
    for loja, it in brutos.values():
        o = montar_oferta(loja, it)
        if not o or o.periodo(agora) == "expirada":
            continue
        k = (o.loja, o.nome_norm, o.preco, o.inicio.date() if o.inicio else None)
        if k in vistos:
            continue
        vistos.add(k)
        ofertas.append(o)
    return ofertas, falhas, n_folhetos


# ----------------------------------------------------------------- cesta básica
@dataclass
class ItemLista:
    secao_pt: str
    secao_fr: str
    pt: str
    fr: str
    alternativas: list[str]
    excluir: list[str]

    def __post_init__(self):
        self._regs = [r for r in ([rx_palavra(w) for w in palavras(a)] for a in self.alternativas) if r]
        self._excl = [rx_prefixo(w) for w in self.excluir]

    @property
    def rotulo(self) -> str:
        return self.pt

    def casa(self, o: Oferta) -> bool:
        n = o.nome_norm
        if any(e.search(n) for e in self._excl):
            return False
        return any(all(r.search(n) for r in regs) for regs in self._regs)


def _dois_nomes(texto: str) -> tuple[str, str]:
    partes = [p.strip() for p in texto.split("|")]
    pt = partes[0]
    return pt, (partes[1] if len(partes) > 1 and partes[1] else pt)


def ler_lista(caminho: Path) -> list[ItemLista]:
    itens, sec = [], ("Minha lista", "Ma liste")
    for linha in caminho.read_text("utf-8").splitlines():
        l = linha.strip()
        if not l or l.startswith("#"):
            continue
        if l.startswith("[") and l.endswith("]"):
            sec = _dois_nomes(l[1:-1])
            continue
        rotulo, resto = (l.split("=", 1) if "=" in l else (l, l))
        pt, fr = _dois_nomes(rotulo)
        tokens = resto.split()
        excluir = [t[1:] for t in tokens if t.startswith("-") and len(t) > 1]
        texto = " ".join(t for t in tokens if not (t.startswith("-") and len(t) > 1))
        alternativas = [a.strip() for a in texto.split("|") if a.strip()]
        if alternativas:
            itens.append(ItemLista(sec[0], sec[1], pt, fr, alternativas, excluir))
    return itens


def melhor(ofertas):
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
        achadas = [o for o in pool if item.casa(o)]
        ord_atual, lojas_atual = melhor([o for o in achadas if o.periodo(agora) == "atual"])
        ord_prox, lojas_prox = melhor([o for o in achadas if o.periodo(agora) == "proxima"])
        dica = ""
        if ord_atual and ord_prox:
            a, p = ord_atual[0].chave_preco(), ord_prox[0].chave_preco()
            if a[0] == p[0] and p[1] < a[1] * 0.97:
                dica = f"espere: mais barato a partir de {dia(ord_prox[0].inicio)} no {ord_prox[0].loja}"
        elif ord_prox and not ord_atual:
            dica = f"entra em promoção {dia(ord_prox[0].inicio)} no {ord_prox[0].loja}"
        resultados.append({"item": item, "atual": ord_atual, "lojas_atual": lojas_atual,
                           "proxima": ord_prox, "lojas_proxima": lojas_prox, "dica": dica})
    return resultados


def roteiro(resultados, periodo):
    por_loja = defaultdict(list)
    for r in resultados:
        for loja in sorted(r["lojas_" + periodo]):
            of = next(o for o in r[periodo] if o.loja == loja)
            por_loja[loja].append((r["item"], of))
    return dict(sorted(por_loja.items(), key=lambda kv: -len(kv[1])))


# ----------------------------------------------------------------- exportação
def _r2(v):
    return None if v is None else round(v, 2)


def exportar_dados(cp, agora, pool, lista) -> dict:
    cont = defaultdict(Counter)
    for o in pool:
        if o.inicio and o.fim:
            per = "next" if o.periodo(agora) == "proxima" else "cur"
            cont[(o.loja, per)][(o.inicio.date().isoformat(), o.fim.date().isoformat())] += 1
    periodos = {l: {per: (list(cont[(l, per)].most_common(1)[0][0]) if cont[(l, per)] else None)
                    for per in ("cur", "next")} for l in LOJAS}

    ofertas = []
    for o in sorted(pool, key=lambda x: (x.loja, x.nome_norm)):
        ofertas.append({
            "i": o.id, "s": o.loja, "n": nome_exibicao(o.nome), "b": o.marca,
            "k": o.nome_norm, "c": o.categoria,
            "p": _r2(o.preco), "q": o.qtd, "u": o.unidade, "pu": _r2(o.preco_un),
            "kg": _r2(o.preco_kg), "ka": o.kg_aprox, "o": _r2(o.preco_original),
            "d": round(o.desconto * 100) if o.desconto else None,
            "t": o.obs(),
            "f": o.inicio.date().isoformat() if o.inicio else None,
            "e": o.fim.date().isoformat() if o.fim else None,
            "w": "next" if o.periodo(agora) == "proxima" else "cur",
            "img": o.imagem,
        })
    return {
        "v": 2,
        "gerado": agora.isoformat(timespec="minutes"),
        "cp": cp,
        "lojas": [{"nome": l, "cor": c["cor"], "endereco": c["endereco"]} for l, c in LOJAS.items()],
        "periodos": periodos,
        "cesta": [{"pt": it.pt, "fr": it.fr, "sec_pt": it.secao_pt, "sec_fr": it.secao_fr,
                   "termos": it.alternativas, "excl": it.excluir} for it in lista],
        "ofertas": ofertas,
    }


def gerar_app(modelo: str, dados: dict) -> str:
    js = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    if "/*__DADOS__*/null" not in modelo:
        raise SystemExit("modelo_app.html não tem o marcador /*__DADOS__*/null")
    return modelo.replace("/*__DADOS__*/null", js, 1)


# ----------------------------------------------------------------- main
def configurar_saida():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    configurar_saida()
    ap = argparse.ArgumentParser(description="Aubaines Val-Bélair: Maxi, Metro, IGA e Super C.")
    ap.add_argument("--cp", default=CODIGO_POSTAL)
    ap.add_argument("--lista", default=str(PASTA / "lista_compras.txt"))
    ap.add_argument("--modelo", default=str(PASTA / "modelo_app.html"))
    ap.add_argument("--saida", default=str(PASTA / "relatorio_promocoes.html"))
    ap.add_argument("--csv", action="store_true")
    ap.add_argument("--sem-cache", action="store_true")
    ap.add_argument("--sem-destaques", action="store_true", help=argparse.SUPPRESS)  # compatibilidade
    ap.add_argument("--nao-abrir", action="store_true")
    args = ap.parse_args(argv)

    cp = re.sub(r"\s+", "", args.cp).upper()
    if not re.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d", cp):
        print(f"Código postal inválido: {args.cp}  (exemplo: G3K2S5)")
        return 2
    for p in (args.lista, args.modelo):
        if not Path(p).exists():
            print(f"Arquivo não encontrado: {p}  (deixe-o na mesma pasta do script)")
            return 2
    lista = ler_lista(Path(args.lista))

    termos = []
    for t in [a for it in lista for a in it.alternativas] + BUSCAS_EXTRAS + [l.lower() for l in LOJAS]:
        if norm(t) not in [norm(x) for x in termos]:
            termos.append(t)

    agora = dt.datetime.now().astimezone()
    print(f"Coletando circulaires para {cp}...")
    try:
        pool, falhas, n_folhetos = coletar(cp, termos, not args.sem_cache, agora)
    except ErroFonte as ex:
        print("\n" + str(ex))
        return 1
    if not pool:
        print("\nNenhuma oferta encontrada (sem internet, código postal errado ou a fonte mudou).")
        return 1

    dados = exportar_dados(cp, agora, pool, lista)
    saida = Path(args.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(gerar_app(Path(args.modelo).read_text("utf-8"), dados), "utf-8")
    (saida.parent / "dados.json").write_text(
        json.dumps(dados, ensure_ascii=False, separators=(",", ":")), "utf-8")

    resultados = analisar(lista, pool, agora)
    if args.csv:
        arq_csv = saida.with_suffix(".csv")
        with arq_csv.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["item", "semaine", "magasin", "produit", "prix", "prix_kg", "rabais_%",
                        "debut", "fin", "note"])
            for r in resultados:
                for per in ("atual", "proxima"):
                    for o in r[per]:
                        w.writerow([r["item"].fr, "actuelle" if per == "atual" else "prochaine",
                                    o.loja, o.nome, o.preco_txt(),
                                    f"{o.preco_kg:.2f}".replace(".", ",") if o.preco_kg else "",
                                    round(o.desconto * 100) if o.desconto else "",
                                    o.inicio.date() if o.inicio else "",
                                    o.fim.date() if o.fim else "", o.obs()])

    por_loja = Counter(o.loja for o in pool)
    print(f"\n{len(pool)} ofertas de {n_folhetos} folhetos: "
          + ", ".join(f"{l} {por_loja.get(l, 0)}" for l in LOJAS))
    for nome_per, per in (("ESTA SEMANA", "atual"), ("PRÓXIMA SEMANA", "proxima")):
        rot = roteiro(resultados, per)
        if rot:
            print(f"\nCESTA BÁSICA — {nome_per}")
            for loja, itens in rot.items():
                print(f"  {loja:<8} {len(itens):>2} {'item ' if len(itens) == 1 else 'itens'}: "
                      + ", ".join(it.pt for it, _ in itens))
    dicas = [r for r in resultados if r["dica"]]
    if dicas:
        print("\nQUANDO COMPRAR")
        for r in dicas:
            print(f"  {r['item'].pt}: {r['dica']}")
    if falhas:
        print(f"\nAtenção: {len(falhas)} buscas falharam: {', '.join(falhas[:10])}")
    print(f"\nApp gerado: {saida}")
    if not args.nao_abrir:
        try:
            webbrowser.open(saida.resolve().as_uri())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
