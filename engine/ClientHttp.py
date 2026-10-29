import logging
import random
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from engine.erros.BloqueioDetectado import Bloqueio

registro_log = logging.getLogger("motor_de_raspagem")

HEADERS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

TIMEOUT_CONEXAO_SEGUNDOS = 10
TIMEOUT_LEITURA_SEGUNDOS = 30

# 1. Pausa quando o site responde 429 (Too Many Requests)
PAUSA_APOS_429_SEGUNDOS = 180
MAXIMO_PAUSAS_SEGUIDAS_POR_429 = 3


class ClienteHttp:
    # 2. Só cuida de rede: sessão, espera entre requisições, 429 e bloqueio.
    # Não sabe nada de imóvel: quem decide o que fazer com 404 ou com o HTML é o motor.
    def __init__(self, segundos_espera_minimo=2, segundos_espera_maximo=4):
        self.segundos_espera_minimo = segundos_espera_minimo
        self.segundos_espera_maximo = segundos_espera_maximo
        self.ja_fez_requisicao = False
        self.sessao_http = self.criar_sessao_http()

    def criar_sessao_http(self):
        sessao = requests.Session()
        sessao.headers.update(HEADERS_NAVEGADOR)

        politica_retentativa = Retry(
            total=3,
            backoff_factor=2,
            # 429 fica de fora: quem trata é o buscar, com pausa longa
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adaptador = HTTPAdapter(max_retries=politica_retentativa)
        sessao.mount("https://", adaptador)
        sessao.mount("http://", adaptador)
        return sessao

    def esperar_intervalo_entre_requisicoes(self):
        # Espera aleatória antes de toda requisição, menos a primeira da execução
        if self.ja_fez_requisicao:
            time.sleep(random.uniform(self.segundos_espera_minimo, self.segundos_espera_maximo))
        self.ja_fez_requisicao = True

    def buscar(self, url):
        pausas_por_429_nesta_url = 0

        while True:
            self.esperar_intervalo_entre_requisicoes()

            inicio = time.monotonic()
            resposta = self.sessao_http.get(
                url, timeout=(TIMEOUT_CONEXAO_SEGUNDOS, TIMEOUT_LEITURA_SEGUNDOS)
            )
            duracao_ms = int((time.monotonic() - inicio) * 1000)
            registro_log.debug(
                "GET %s -> status %s, %s bytes, %s ms",
                url, resposta.status_code, len(resposta.content), duracao_ms,
            )

            if resposta.status_code != 429:
                break

            # 3. 429 = site pedindo calma. Descansa e tenta a MESMA url de novo.
            # Se o site mandar Retry-After maior que a pausa padrão, respeita o dele
            pausas_por_429_nesta_url += 1
            if pausas_por_429_nesta_url > MAXIMO_PAUSAS_SEGUIDAS_POR_429:
                raise Bloqueio(
                    f"429 persistente: {MAXIMO_PAUSAS_SEGUIDAS_POR_429} pausas seguidas não bastaram em {url}"
                )

            retry_after = resposta.headers.get("Retry-After", "")
            segundos_de_pausa = PAUSA_APOS_429_SEGUNDOS
            if retry_after.isdigit():
                segundos_de_pausa = max(PAUSA_APOS_429_SEGUNDOS, int(retry_after))

            registro_log.warning(
                "429 Too Many Requests (Retry-After=%r). Pausa de %s s e nova tentativa (%s/%s)",
                retry_after, segundos_de_pausa,
                pausas_por_429_nesta_url, MAXIMO_PAUSAS_SEGUIDAS_POR_429,
            )
            time.sleep(segundos_de_pausa)

        # 4. 401/403 é bloqueio: não adianta tentar de novo
        if resposta.status_code in (401, 403):
            raise Bloqueio(f"Status {resposta.status_code} em {url}")

        return resposta

    def fechar(self):
        self.sessao_http.close()