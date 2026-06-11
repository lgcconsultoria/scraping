"""Cliente HTTP com rate-limit global e backoff exponencial. Independente de tribunal."""
import logging
import time

try:
    import requests
except ImportError:
    import sys
    sys.exit("Este módulo requer 'requests' (pip install requests).")


class ClienteHttp:
    """Sessão HTTP com rate limit global e backoff exponencial em 429/5xx."""

    def __init__(self, user_agent: str, min_intervalo: float = 2.0, max_tentativas: int = 5):
        self.sessao = requests.Session()
        self.sessao.headers.update({"User-Agent": user_agent})
        self._ultimo = 0.0
        self._min_intervalo = min_intervalo
        self._max_tentativas = max_tentativas

    def _respeita_intervalo(self):
        falta = self._min_intervalo - (time.monotonic() - self._ultimo)
        if falta > 0:
            time.sleep(falta)

    def requisitar(self, metodo, url, **kw):
        kw.setdefault("timeout", 60)
        ultima_exc = None
        for tentativa in range(1, self._max_tentativas + 1):
            self._respeita_intervalo()
            try:
                resposta = self.sessao.request(metodo, url, **kw)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._ultimo = time.monotonic()
                ultima_exc = exc
                if tentativa < self._max_tentativas:
                    espera = min(60, 2 * 2 ** tentativa)
                    logging.warning("erro de rede (%s/%s) em %s: %s; aguardando %ss",
                                    tentativa, self._max_tentativas, url, exc, espera)
                    time.sleep(espera)
                continue
            self._ultimo = time.monotonic()
            if resposta.status_code in (429, 500, 502, 503, 504) and tentativa < self._max_tentativas:
                espera = min(60, 2 * 2 ** tentativa)
                retry_after = resposta.headers.get("Retry-After", "").strip()
                if retry_after.isdigit():
                    espera = max(espera, int(retry_after))
                logging.warning("HTTP %s (%s/%s) em %s; aguardando %ss",
                                resposta.status_code, tentativa, self._max_tentativas, url, espera)
                resposta.close()
                time.sleep(espera)
                continue
            resposta.raise_for_status()
            return resposta
        raise ultima_exc if ultima_exc else RuntimeError(f"tentativas esgotadas: {url}")

    def get(self, url, **kw):
        return self.requisitar("GET", url, **kw)

    def post(self, url, **kw):
        return self.requisitar("POST", url, **kw)


def texto_resposta(resposta):
    """Texto da resposta corrigindo charset ausente (portais legados usam latin-1)."""
    if "charset" not in resposta.headers.get("Content-Type", "").lower():
        resposta.encoding = resposta.apparent_encoding or resposta.encoding
    return resposta.text


def classificar_resposta(resposta):
    """Lê o primeiro chunk de uma resposta em streaming e identifica o formato.

    Retorna (formato, primeiro_chunk, iterador) onde formato é "pdf", "rtf" ou "".
    Usa Content-Type e magic bytes; acórdãos do eproc chegam como RTF.
    """
    iterador = resposta.iter_content(8192)
    primeiro = b""
    for chunk in iterador:
        if chunk:
            primeiro = chunk
            break
    ct = resposta.headers.get("Content-Type", "").lower()
    inicio = primeiro.lstrip()
    if "pdf" in ct or inicio.startswith(b"%PDF"):
        formato = "pdf"
    elif inicio.startswith(b"{\\rtf") or "rtf" in ct or "msword" in ct:
        formato = "rtf"
    else:
        formato = ""
    return formato, primeiro, iterador
