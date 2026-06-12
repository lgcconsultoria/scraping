"""Gate anti-alucinação: valida citações legais em três dimensões.

Checks disponíveis (cada um retorna dict com "ok" e "msg"):
  1. existencia — o artigo citado existe no cache local da lei
  2. verbatim   — o trecho citado coincide com o texto armazenado (Jaccard >= limiar)
  3. vigencia   — o cache local foi atualizado nos últimos VALIDADE_CACHE_DIAS dias

Uso:
    from qa.gate import verificar_citacao

    resultado = verificar_citacao("CPC", "300", "O juiz poderá conceder...")
    if resultado["aprovado"]:
        ...
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from leis.planalto import artigo_texto, carregar_lei

VALIDADE_CACHE_DIAS = 30


def _normalizar(texto: str) -> str:
    """Remove acentos, pontuação e espaços redundantes; retorna minúsculas."""
    texto = unicodedata.normalize("NFKD", texto)
    texto = texto.encode("ascii", "ignore").decode()
    texto = re.sub(r"[^\w\s]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip().lower()


# ------------------------------------------------------------------
# Checks individuais
# ------------------------------------------------------------------

def verificar_existencia(
    lei: str, artigo: str | int, diretorio: str = "leis/dados"
) -> dict:
    """Verifica se o artigo existe no cache local da lei."""
    texto = artigo_texto(lei, artigo, diretorio)
    existe = bool(texto)
    return {
        "check": "existencia",
        "ok": existe,
        "lei": lei,
        "artigo": str(artigo),
        "msg": "" if existe else f"Art. {artigo} não encontrado em {lei}",
    }


def verificar_verbatim(
    lei: str,
    artigo: str | int,
    citacao: str,
    limiar: float = 0.85,
    diretorio: str = "leis/dados",
) -> dict:
    """Verifica se a citação corresponde ao texto real (similaridade Jaccard de tokens)."""
    texto_real = artigo_texto(lei, artigo, diretorio)
    if not texto_real:
        return {
            "check": "verbatim",
            "ok": False,
            "similaridade": 0.0,
            "msg": f"Art. {artigo} não encontrado em {lei}; impossível verificar verbatim",
        }

    ref = set(_normalizar(texto_real).split())
    cit = set(_normalizar(citacao).split())

    if not cit:
        return {
            "check": "verbatim",
            "ok": False,
            "similaridade": 0.0,
            "msg": "citação vazia",
        }

    uniao = ref | cit
    similaridade = len(ref & cit) / len(uniao) if uniao else 0.0
    ok = similaridade >= limiar
    return {
        "check": "verbatim",
        "ok": ok,
        "similaridade": round(similaridade, 4),
        "limiar": limiar,
        "msg": "" if ok else f"similaridade {similaridade:.1%} abaixo do limiar {limiar:.1%}",
    }


def verificar_vigencia(
    lei: str,
    max_idade_dias: int = VALIDADE_CACHE_DIAS,
    diretorio: str = "leis/dados",
) -> dict:
    """Verifica se o cache local da lei foi atualizado recentemente."""
    dados = carregar_lei(lei, diretorio)
    if dados is None:
        return {
            "check": "vigencia",
            "ok": False,
            "msg": f"{lei} não está no cache local; execute: python leis/planalto.py {lei}",
        }

    atualizado_em = dados.get("atualizado_em", "")
    if not atualizado_em:
        return {"check": "vigencia", "ok": False, "msg": "campo atualizado_em ausente"}

    try:
        data_cache = date.fromisoformat(atualizado_em[:10])
    except ValueError:
        return {
            "check": "vigencia",
            "ok": False,
            "msg": f"formato de data inválido: {atualizado_em}",
        }

    idade = (date.today() - data_cache).days
    ok = idade <= max_idade_dias
    return {
        "check": "vigencia",
        "ok": ok,
        "atualizado_em": atualizado_em[:10],
        "idade_dias": idade,
        "max_idade_dias": max_idade_dias,
        "msg": "" if ok else f"cache com {idade} dias > máximo {max_idade_dias} dias",
    }


# ------------------------------------------------------------------
# Check consolidado
# ------------------------------------------------------------------

def verificar_citacao(
    lei: str,
    artigo: str | int,
    citacao: str,
    *,
    limiar_verbatim: float = 0.85,
    max_idade_dias: int = VALIDADE_CACHE_DIAS,
    diretorio: str = "leis/dados",
) -> dict:
    """Executa os 3 checks e retorna resultado consolidado.

    Returns:
        {
            "aprovado": bool,
            "lei": str,
            "artigo": str,
            "checks": [existencia, verbatim, vigencia],
            "reprovados": [nomes dos checks que falharam],
        }
    """
    checks = [
        verificar_existencia(lei, artigo, diretorio),
        verificar_verbatim(lei, artigo, citacao, limiar_verbatim, diretorio),
        verificar_vigencia(lei, max_idade_dias, diretorio),
    ]
    reprovados = [c["check"] for c in checks if not c["ok"]]
    return {
        "aprovado": not reprovados,
        "lei": lei,
        "artigo": str(artigo),
        "checks": checks,
        "reprovados": reprovados,
    }
