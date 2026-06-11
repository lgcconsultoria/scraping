"""Extração de texto de documentos jurídicos (.pdf, .rtf, .html) com fallbacks."""
import html as html_mod
import re

TAG_RE = re.compile(r"<[^>]+>")


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
