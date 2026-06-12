"""Repositório local de textos legais (CPC, CC, Lei de Alimentos).

Uso típico (Phase 5 — gate anti-alucinação):

    from leis.planalto import carregar_lei, artigo_texto

    dados = carregar_lei("CPC")          # None se não cacheado
    texto = artigo_texto("CPC", "300")   # str ou ""
"""
from leis.planalto import LEIS, carregar_lei, artigo_texto  # noqa: F401
