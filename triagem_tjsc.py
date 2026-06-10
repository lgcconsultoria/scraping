#!/usr/bin/env python3
"""Triagem offline dos acórdãos baixados pelo scraper_tjsc.py.

Lê <saida>/index.csv, extrai o texto de cada documento baixado (.pdf, .rtf —
formato em que o eproc entrega os inteiros teores — e .html), pontua termos da
tese jurídica em dois eixos e classifica cada decisão como FAVORAVEL /
CONTRARIA / NEUTRA por eixo, gerando dois artefatos derivados (sobrescritos a
cada execução):

  <saida>/triagem.csv            - uma linha por documento, com scores e termos
  <saida>/relatorio_triagem.md   - relatório priorizado para leitura

AVISO: classificação automática por palavra-chave — ferramenta de priorização
de leitura, NÃO juízo jurídico. Um acórdão pode citar um termo para afastá-lo.
Decisões com sinal CONTRÁRIO em qualquer eixo entram na seção de ALERTA no
topo do relatório para leitura prioritária (não para descarte).

Uso:
    python triagem_tjsc.py              # processa decisoes_tjsc/
    python triagem_tjsc.py --out DIR    # pasta de saída alternativa do scraper
    python triagem_tjsc.py --ocr        # tenta OCR em PDFs sem texto extraível
                                        # (requer pytesseract + pdf2image)

Este script é 100% offline: opera apenas sobre os arquivos já baixados.
Dicionários de termos, pesos e limiares ficam no topo, fáceis de ajustar.
"""

import argparse
import collections
import csv
import datetime
import html as html_mod
import logging
import os
import re
import sys
import unicodedata

OUT_PADRAO = "decisoes_tjsc"
LIMIAR_FAV = 3      # score >= LIMIAR_FAV  -> FAVORAVEL
LIMIAR_CON = -3     # score <= LIMIAR_CON  -> CONTRARIA
MIN_TEXTO = 40      # menos caracteres que isso = sem texto extraível (escaneado)

# Termos por eixo. Pesos favoráveis somam, contrários subtraem. Regras de
# casamento (ver termo_para_regex): minúsculas e sem acento; a 1ª palavra exige
# fronteira ("deferi" NÃO dispara dentro de "indeferiu", "provido" NÃO dispara
# em "desprovido"); sufixos são livres ("nao comprovad" casa "não comprovada");
# "..." tolera até 6 palavras entre as partes.
EIXOS_TRIAGEM = {
    "eixo1": {
        "rotulo": "Eixo 1 — Prova indispensável / cognição sumária",
        "favoravel": {
            "imprescindibilidade de dilacao probatoria": 3,
            "necessidade de dilacao probatoria": 3,
            "inexistencia de prova": 3,
            "ausencia de prova": 2,
            "carencia de demonstracao": 2,
            "nao comprovad": 2,
            "renda ... desconhecida": 2,
            "prova pre-constituida": 2,
            "mera alegacao": 2,
            "narrativa unilateral": 2,
            "documento unilateral": 2,
            "sem contraditorio": 1,
            "indeferi": 1,
            "improvido": 1,
            "desprovido": 1,
        },
        "contrario": {
            "verossimilhanca": 2,
            "prova inequivoca presente": 2,
            "deferi": 1,            # com fronteira: não casa "indeferiu"
            "concedi": 1,
            "provido o recurso": 2,
            "majora": 1,            # ambíguo (depende de quem recorreu)
        },
    },
    "eixo2": {
        "rotulo": "Eixo 2 — Binômio / capacidade contributiva",
        "favoravel": {
            "binomio": 3,
            "possibilidade do alimentante": 3,
            "capacidade contributiva": 3,
            "renda liquida": 2,
            "lucro liquido": 2,
            "faturamento bruto nao reflete": 3,
            "pro-labore": 2,
            "art. 1.694": 2,
            "reducao": 1,           # ambíguo (depende de quem recorreu)
            "minora": 1,
        },
        "contrario": {
            "sinais exteriores de riqueza": 3,
            "presuncao de capacidade": 2,
            "faturamento ... parametro": 2,
            "majora": 1,
        },
    },
}

COLUNAS = [
    "numero_processo", "relator", "camara", "orgao_julgador", "data_julgamento",
    "arquivo", "formato", "precisa_ocr",
    "score_eixo1", "classe_eixo1", "termos_eixo1",
    "score_eixo2", "classe_eixo2", "termos_eixo2",
    "alerta_contrario",
]

TAG_RE = re.compile(r"<[^>]+>")


def normalizar(texto):
    """Minúsculas, sem acento, espaços uniformes — preservando o COMPRIMENTO
    (1 caractere por caractere), para que posições de match no texto
    normalizado sirvam para recortar snippets do texto original."""
    saida = []
    for ch in texto:
        if ch.isspace():
            saida.append(" ")
            continue
        decomposto = unicodedata.normalize("NFKD", ch)
        base = next((c for c in decomposto if not unicodedata.combining(c)), " ")
        base = base.lower()
        saida.append(base if base.isascii() else " ")
    return "".join(saida)


def termo_para_regex(termo):
    def parte(trecho):
        palavras = normalizar(trecho).split()
        return r"\s+".join(re.escape(palavra) for palavra in palavras)
    if "..." in termo:
        esquerda, direita = (parte(lado) for lado in termo.split("...", 1))
        return re.compile(r"\b" + esquerda + r"\W+(?:\w+\W+){0,6}" + direita)
    return re.compile(r"\b" + parte(termo))


def compilar_regras():
    regras = {}
    for chave, eixo in EIXOS_TRIAGEM.items():
        lista = []
        for sinal, multiplicador in (("favoravel", 1), ("contrario", -1)):
            for termo, peso in eixo[sinal].items():
                lista.append((sinal, termo, multiplicador * peso, termo_para_regex(termo)))
        regras[chave] = lista
    return regras


def _texto_pdf(caminho):
    # BaseException: bibliotecas com binário nativo quebrado podem estourar
    # erros fora da hierarquia de Exception já no import.
    erros = []
    try:
        import pdfplumber
        with pdfplumber.open(caminho) as pdf:
            return "\n".join(pagina.extract_text() or "" for pagina in pdf.pages)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        erros.append(f"pdfplumber: {exc.__class__.__name__}: {exc}")
    try:
        from pypdf import PdfReader
        return "\n".join(pagina.extract_text() or "" for pagina in PdfReader(caminho).pages)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        erros.append(f"pypdf: {exc.__class__.__name__}: {exc}")
    raise RuntimeError("falha ao extrair PDF (instale: pip install pdfplumber pypdf) | "
                       + " | ".join(erros))


def _rtf_minimo(texto_rtf):
    """Conversor RTF->texto de emergência (quando striprtf não está instalado)."""
    texto = re.sub(r"\\'([0-9a-fA-F]{2})",
                   lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"),
                   texto_rtf)
    texto = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), texto)
    texto = re.sub(r"\\(?:par|line)\b", "\n", texto)
    texto = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", texto)
    texto = re.sub(r"\\.", " ", texto)
    return texto.replace("{", " ").replace("}", " ")


def _texto_rtf(caminho):
    with open(caminho, "rb") as arq:
        bruto = arq.read().decode("latin-1", errors="replace")
    try:
        from striprtf.striprtf import rtf_to_text
        return rtf_to_text(bruto, errors="ignore")
    except Exception:
        return _rtf_minimo(bruto)


def _texto_html(caminho):
    with open(caminho, encoding="utf-8", errors="ignore") as arq:
        conteudo = arq.read()
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(conteudo, "html.parser").get_text(" ")
    except ImportError:
        return html_mod.unescape(TAG_RE.sub(" ", conteudo))


def extrair_texto(caminho):
    baixo = caminho.lower()
    if baixo.endswith(".pdf"):
        return _texto_pdf(caminho)
    if baixo.endswith(".rtf"):
        return _texto_rtf(caminho)
    return _texto_html(caminho)


def tentar_ocr(caminho):
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("OCR requer: pip install pytesseract pdf2image "
                           "(e os binários tesseract e poppler)") from exc
    paginas = convert_from_path(caminho)
    return "\n".join(pytesseract.image_to_string(pagina, lang="por") for pagina in paginas)


def _snippet(texto_original, inicio, fim, raio=60):
    janela = texto_original[max(0, inicio - raio):min(len(texto_original), fim + raio)]
    return re.sub(r"\s+", " ", janela).strip()


def avaliar(texto_norm, texto_original, regras_eixo):
    """Retorna (score, classe, disparos). Cada termo conta uma vez (presença);
    o snippet vem da primeira ocorrência, no texto original (com acentos)."""
    score, disparos = 0, []
    for sinal, termo, peso, rx in regras_eixo:
        m = rx.search(texto_norm)
        if m:
            score += peso
            disparos.append({"sinal": sinal, "termo": termo, "peso": peso,
                             "snippet": _snippet(texto_original, m.start(), m.end())})
    if score >= LIMIAR_FAV:
        classe = "FAVORAVEL"
    elif score <= LIMIAR_CON:
        classe = "CONTRARIA"
    else:
        classe = "NEUTRA"
    return score, classe, disparos


def _termos_compactos(disparos):
    return "; ".join(f"{d['termo']} ({d['peso']:+d})" for d in disparos)


def _chave_data(valor):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", valor or "")
    return (m.group(3), m.group(2), m.group(1)) if m else ("0000", "00", "00")


def _linha_doc(registro, chave_eixo=None):
    base = (f"- **{registro['numero_processo'] or registro['arquivo'] or '?'}** | "
            f"{registro['relator']} ({registro['camara']}) | {registro['data_julgamento']}")
    if chave_eixo:
        base += (f" | score {registro['score_' + chave_eixo]:+d} | "
                 f"termos: {registro['termos_' + chave_eixo] or '—'}")
    return base


def _primeiro_snippet(registro, chave_eixo, sinal):
    for disparo in registro["_disparos"].get(chave_eixo, []):
        if disparo["sinal"] == sinal:
            return disparo["snippet"]
    return ""


def gerar_relatorio(caminho_md, registros, stats):
    agora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    analisaveis = [r for r in registros if not r["precisa_ocr"]]
    pendentes = [r for r in registros if r["precisa_ocr"]]
    linhas = [
        "# Relatório de Triagem — Acórdãos TJSC",
        "> AVISO: classificação automática por palavra-chave. Ferramenta de priorização",
        "> de leitura, NÃO juízo jurídico. Um acórdão pode citar um termo justamente para",
        "> afastá-lo — confirme cada decisão no inteiro teor.",
        "",
        f"Gerado em: {agora} | Documentos analisados: {len(analisaveis)} | "
        f"Requerem OCR/leitura manual: {len(pendentes)}",
        "",
        "## ⚠️ ALERTA — Precedentes potencialmente CONTRÁRIOS (ler primeiro)",
        "",
    ]
    alertas = [r for r in analisaveis if r["alerta_contrario"]]
    alertas.sort(key=lambda r: _chave_data(r["data_julgamento"]), reverse=True)
    alertas.sort(key=lambda r: min(r["score_eixo1"], r["score_eixo2"]))
    if not alertas:
        linhas.append("Nenhum documento com sinal contrário nos termos atuais.")
    for reg in alertas:
        eixos_contra = [chave for chave in EIXOS_TRIAGEM
                        if reg["classe_" + chave] == "CONTRARIA"]
        linhas.append(_linha_doc(reg) + " | contrária em: "
                      + ", ".join(eixos_contra))
        for chave in eixos_contra:
            trecho = _primeiro_snippet(reg, chave, "contrario")
            linhas.append(f"  - termos ({chave}): {reg['termos_' + chave]}")
            if trecho:
                linhas.append(f"    > “…{trecho}…”")
        linhas.append(f"  Arquivo: `{reg['arquivo']}`")
        linhas.append("")

    for chave, eixo in EIXOS_TRIAGEM.items():
        favoraveis = [r for r in analisaveis if r["classe_" + chave] == "FAVORAVEL"]
        contrarias = [r for r in analisaveis if r["classe_" + chave] == "CONTRARIA"]
        neutras = [r for r in analisaveis if r["classe_" + chave] == "NEUTRA"]
        for grupo in (favoraveis, contrarias, neutras):
            grupo.sort(key=lambda r: _chave_data(r["data_julgamento"]), reverse=True)
            grupo.sort(key=lambda r: r["score_" + chave], reverse=True)
        linhas += ["", f"## {eixo['rotulo']}", ""]
        linhas.append(f"### Favoráveis ({len(favoraveis)})")
        for reg in favoraveis:
            linhas.append(_linha_doc(reg, chave))
            trecho = _primeiro_snippet(reg, chave, "favoravel")
            if trecho:
                linhas.append(f"  > “…{trecho}…”")
            linhas.append(f"  Arquivo: `{reg['arquivo']}`")
        linhas.append("")
        linhas.append(f"### Contrárias ({len(contrarias)}) — detalhadas na seção ALERTA")
        for reg in contrarias:
            linhas.append(_linha_doc(reg, chave))
        linhas.append("")
        linhas.append(f"### Neutras / revisar ({len(neutras)})")
        for reg in neutras:
            linhas.append(_linha_doc(reg, chave) + f" | `{reg['arquivo']}`")

    linhas += ["", "## Apêndice — documentos sem texto extraível ou com erro", ""]
    if not pendentes:
        linhas.append("Nenhum.")
    for reg in pendentes:
        linhas.append(f"- {reg['numero_processo'] or '?'} | `{reg['arquivo'] or '(sem arquivo)'}`"
                      f" | motivo: {reg['precisa_ocr']}")
    linhas.append("")
    with open(caminho_md, "w", encoding="utf-8") as arq:
        arq.write("\n".join(linhas))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Triagem por palavra-chave dos acórdãos baixados pelo scraper_tjsc "
                    "(prioriza leitura; não substitui análise jurídica).")
    parser.add_argument("--out", default=OUT_PADRAO,
                        help=f"pasta de saída do scraper (padrão: {OUT_PADRAO})")
    parser.add_argument("--ocr", action="store_true",
                        help="tenta OCR em PDFs sem texto (requer pytesseract + pdf2image)")
    args = parser.parse_args(argv)

    caminho_index = os.path.join(args.out, "index.csv")
    if not os.path.exists(caminho_index):
        sys.exit(f"{caminho_index} não encontrado — rode antes o scraper_tjsc.py "
                 "(com download, não só --dry-run).")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)

    regras = compilar_regras()
    with open(caminho_index, newline="", encoding="utf-8") as arq:
        catalogo = list(csv.DictReader(arq))

    stats = collections.Counter()
    registros = []
    for linha in catalogo:
        registro = {coluna: linha.get(coluna, "") for coluna in
                    ("numero_processo", "relator", "camara", "orgao_julgador",
                     "data_julgamento", "arquivo", "formato")}
        registro.update(precisa_ocr="", alerta_contrario="", _disparos={})
        for chave in EIXOS_TRIAGEM:
            registro.update({f"score_{chave}": 0, f"classe_{chave}": "",
                             f"termos_{chave}": ""})
        caminho = os.path.join(args.out, registro["arquivo"]) if registro["arquivo"] else ""
        try:
            if not caminho or not os.path.exists(caminho):
                registro["precisa_ocr"] = "arquivo_ausente"
                stats["sem_arquivo"] += 1
            else:
                texto = extrair_texto(caminho)
                if (len(texto.strip()) < MIN_TEXTO and args.ocr
                        and caminho.lower().endswith(".pdf")):
                    try:
                        texto_ocr = tentar_ocr(caminho)
                        if len(texto_ocr.strip()) >= MIN_TEXTO:
                            texto = texto_ocr
                            registro["precisa_ocr"] = "ocr_aplicado"
                            stats["ocr_aplicado"] += 1
                    except Exception as exc:
                        logging.warning("OCR falhou em %s: %s", registro["arquivo"], exc)
                if len(texto.strip()) < MIN_TEXTO:
                    registro["precisa_ocr"] = "True"
                    stats["precisa_ocr"] += 1
                else:
                    texto_norm = normalizar(texto)
                    for chave in EIXOS_TRIAGEM:
                        score, classe, disparos = avaliar(texto_norm, texto, regras[chave])
                        registro[f"score_{chave}"] = score
                        registro[f"classe_{chave}"] = classe
                        registro[f"termos_{chave}"] = _termos_compactos(disparos)
                        registro["_disparos"][chave] = disparos
                        stats[f"{chave}_{classe}"] += 1
                    if any(registro[f"classe_{chave}"] == "CONTRARIA"
                           for chave in EIXOS_TRIAGEM):
                        registro["alerta_contrario"] = "True"
                        stats["alertas"] += 1
                    stats["analisados"] += 1
        except Exception as exc:
            logging.error("%s: %s", registro["arquivo"] or registro["numero_processo"], exc)
            registro["precisa_ocr"] = f"erro:{exc}"
            stats["erros"] += 1
        registros.append(registro)

    caminho_csv = os.path.join(args.out, "triagem.csv")
    with open(caminho_csv, "w", newline="", encoding="utf-8") as arq:
        escritor = csv.DictWriter(arq, fieldnames=COLUNAS, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(registros)
    caminho_md = os.path.join(args.out, "relatorio_triagem.md")
    gerar_relatorio(caminho_md, registros, stats)

    resumo = (f"documentos={len(registros)} | analisados={stats['analisados']} | "
              + " | ".join(f"{chave}: fav={stats[f'{chave}_FAVORAVEL']} "
                           f"con={stats[f'{chave}_CONTRARIA']} "
                           f"neu={stats[f'{chave}_NEUTRA']}"
                           for chave in EIXOS_TRIAGEM)
              + f" | alertas={stats['alertas']} | precisa_ocr={stats['precisa_ocr']} | "
                f"sem_arquivo={stats['sem_arquivo']} | erros={stats['erros']}")
    logging.info("FIM | %s", resumo)
    print(f"\nConcluído. {resumo}")
    print(f"Triagem: {caminho_csv}\nRelatório: {caminho_md}")


if __name__ == "__main__":
    main()
