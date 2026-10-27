import re
import time
import random
import sqlite3
import logging
from datetime import datetime
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("motor_de_raspagem.log", encoding="utf-8"),
    ],
)
registro_log = logging.getLogger("motor_de_raspagem")

class BloqueioDetectado(Exception):
    """O site respondeu 401/403: continuar só pioraria o bloqueio."""

URL_LISTAGEM_BASE = (
    "https://www.vivareal.com.br/venda/sp/sao-paulo/apartamento_residencial/"
    "?onde=%2CS%C3%A3o+Paulo%2CS%C3%A3o+Paulo%2C%2C%2C%2C%2Ccity%2C"
    "BR%3ESao+Paulo%3ENULL%3ESao+Paulo%2C-23.555771%2C-46.639557%2C"
    "&tipos=apartamento_residencial&pagina={pagina}"
)

HEADERS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


PADRAO_SLUG_IMOVEL = re.compile(
    r"/imovel/"
    r"(?P<tipo>[a-z-]+?)-(?P<quartos>\d+)-quartos-"
    r"(?P<local>.+?)-sao-paulo-"
    r"(?P<garagem>com-garagem-)?"
    r"(?P<area>\d+)m2-venda-RS(?P<preco>\d+)-id-(?P<id>\d+)"
)

PADRAO_ZONA = re.compile(r"-(zona-(?:norte|sul|leste|oeste)|centro)")

# 2. Compilado uma vez só, em vez de a cada página
PADRAO_LINK_IMOVEL = re.compile(r'href="([^"]*?/imovel/[^"]*)"')

TIMEOUT_CONEXAO_SEGUNDOS = 10
TIMEOUT_LEITURA_SEGUNDOS = 30


class MotorDeRaspagem:
    def __init__(self, nome_banco="imoveis.db", segundos_espera_minimo=2, segundos_espera_maximo=4):
        self.segundos_espera_minimo = segundos_espera_minimo
        self.segundos_espera_maximo = segundos_espera_maximo

        self.sessao_http = self.criar_sessao_http()

        # 3. Banco SQLite: o id do imóvel é a chave primária, então repetido é ignorado
        self.conexao_banco = sqlite3.connect(nome_banco)
        self.criar_tabela_imoveis()

    # 4. Permite usar "with MotorDeRaspagem() as motor", fechando tudo no final
    def __enter__(self):
        return self

    def __exit__(self, tipo_excecao, valor_excecao, rastreio):
        self.fechar()
        return False

    def fechar(self):
        self.sessao_http.close()
        self.conexao_banco.close()

    def criar_sessao_http(self):
        sessao = requests.Session()
        sessao.headers.update(HEADERS_NAVEGADOR)

        # 5. Retentativa automática com espera crescente (2s, 4s, 8s) para erros
        # temporários. Respeita o header Retry-After quando o site manda
        politica_retentativa = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adaptador = HTTPAdapter(max_retries=politica_retentativa)
        sessao.mount("https://", adaptador)
        sessao.mount("http://", adaptador)
        return sessao

    def criar_tabela_imoveis(self):
        with self.conexao_banco:
            self.conexao_banco.execute(
                """
                CREATE TABLE IF NOT EXISTS imoveis (
                    id TEXT PRIMARY KEY,
                    url TEXT,
                    tipo TEXT,
                    quartos INTEGER,
                    bairro TEXT,
                    zona TEXT,
                    tem_garagem INTEGER,
                    area_m2 INTEGER,
                    preco INTEGER,
                    pagina_descoberta INTEGER,
                    data_coleta TEXT
                )
                """
            )

    def buscar_pagina_listagem(self, numero_pagina):
        url_pagina = URL_LISTAGEM_BASE.format(pagina=numero_pagina)
        resposta = self.sessao_http.get(
            url_pagina, timeout=(TIMEOUT_CONEXAO_SEGUNDOS, TIMEOUT_LEITURA_SEGUNDOS)
        )

        # 6. 401/403 é bloqueio: não adianta tentar de novo, então avisa e para
        if resposta.status_code in (401, 403):
            raise BloqueioDetectado(f"Status {resposta.status_code} na página {numero_pagina}")

        resposta.raise_for_status()
        return resposta.text

    def extrair_links_imoveis(self, html):
        # 7. Remove o "?source=..." para o mesmo imóvel não aparecer com URLs diferentes
        links_brutos = PADRAO_LINK_IMOVEL.findall(html)
        links_limpos = {link.split("?")[0] for link in links_brutos}
        return sorted(links_limpos)

    def extrair_dados_da_url(self, url_imovel):
        correspondencia = PADRAO_SLUG_IMOVEL.search(url_imovel)
        if correspondencia is None:
            return None

        local_bruto = correspondencia.group("local")

        # 8. A zona vem colada no bairro: "vila-madalena-zona-oeste"
        correspondencia_zona = PADRAO_ZONA.search(local_bruto)
        if correspondencia_zona:
            bairro = local_bruto[: correspondencia_zona.start()]
            zona = correspondencia_zona.group(1)
        else:
            bairro = local_bruto
            zona = None

        return {
            "id": correspondencia.group("id"),
            "url": url_imovel,
            "tipo": correspondencia.group("tipo"),
            "quartos": int(correspondencia.group("quartos")),
            "bairro": bairro,
            "zona": zona,
            "tem_garagem": 1 if correspondencia.group("garagem") else 0,
            "area_m2": int(correspondencia.group("area")),
            "preco": int(correspondencia.group("preco")),
        }

    def salvar_imovel(self, dados_imovel, numero_pagina):
        # 9. Sem commit aqui: quem chama agrupa a página inteira numa transação só
        cursor = self.conexao_banco.execute(
            """
            INSERT OR IGNORE INTO imoveis
            (id, url, tipo, quartos, bairro, zona, tem_garagem, area_m2, preco,
             pagina_descoberta, data_coleta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dados_imovel["id"], dados_imovel["url"], dados_imovel["tipo"],
                dados_imovel["quartos"], dados_imovel["bairro"], dados_imovel["zona"],
                dados_imovel["tem_garagem"], dados_imovel["area_m2"], dados_imovel["preco"],
                numero_pagina, datetime.now().isoformat(),
            ),
        )
        # rowcount é 1 se inseriu e 0 se o id já existia
        return cursor.rowcount == 1

    def salvar_html_para_depuracao(self, numero_pagina, html):
        nome_arquivo = f"depuracao_pagina_{numero_pagina}.html"
        with open(nome_arquivo, "w", encoding="utf-8") as arquivo:
            arquivo.write(html)
        registro_log.warning("HTML da página %s salvo em %s para análise", numero_pagina, nome_arquivo)

    def processar_pagina(self, numero_pagina, links_imoveis):
        novos_nesta_pagina = 0
        sem_padrao_nesta_pagina = 0

        # 10. "with" na conexão = uma transação: ou grava a página toda ou nada.
        # Se o programa cair no meio, não sobra página pela metade
        with self.conexao_banco:
            for url_imovel in links_imoveis:
                dados_imovel = self.extrair_dados_da_url(url_imovel)
                if dados_imovel is None:
                    sem_padrao_nesta_pagina += 1
                    registro_log.warning("URL fora do padrão: %s", url_imovel)
                    continue
                if self.salvar_imovel(dados_imovel, numero_pagina):
                    novos_nesta_pagina += 1

        return novos_nesta_pagina, sem_padrao_nesta_pagina

    def raspar_paginas(self, pagina_inicial=1, pagina_final=3):
        total_novos = 0

        for numero_pagina in range(pagina_inicial, pagina_final + 1):
            # 11. Falha de rede ou bloqueio para a coleta de forma limpa e diz
            # de qual página retomar, em vez de estourar um traceback
            try:
                html = self.buscar_pagina_listagem(numero_pagina)
            except BloqueioDetectado as erro:
                registro_log.error("Bloqueio detectado (%s). Retome da página %s depois.", erro, numero_pagina)
                break
            except requests.RequestException as erro:
                registro_log.error("Falha de rede na página %s: %s. Retome daqui depois.", numero_pagina, erro)
                break

            links_imoveis = self.extrair_links_imoveis(html)

            # 12. Zero links com status 200 é suspeito (página de desafio, layout novo).
            # Guarda o HTML para você abrir e ver o que veio
            if not links_imoveis:
                registro_log.warning("Página %s sem nenhum link de imóvel", numero_pagina)
                self.salvar_html_para_depuracao(numero_pagina, html)

            novos_nesta_pagina, sem_padrao_nesta_pagina = self.processar_pagina(numero_pagina, links_imoveis)
            total_novos += novos_nesta_pagina

            registro_log.info(
                "Página %s: %s links, %s novos, %s fora do padrão",
                numero_pagina, len(links_imoveis), novos_nesta_pagina, sem_padrao_nesta_pagina,
            )

            # 13. Para se a página não trouxe nada de novo (fim da paginação ou repetição)
            if novos_nesta_pagina == 0:
                registro_log.info("Nenhum imóvel novo, parando.")
                break

            # 14. Espera aleatória entre as páginas para não martelar o servidor
            time.sleep(random.uniform(self.segundos_espera_minimo, self.segundos_espera_maximo))

        registro_log.info("Fim da coleta: %s imóveis novos no total", total_novos)
        return total_novos



