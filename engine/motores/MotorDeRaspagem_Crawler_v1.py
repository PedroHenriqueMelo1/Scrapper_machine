import re
import time
import random
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import os
from dotenv import load_dotenv
from engine.modelos.TiposMotor import Faixa
from engine.erros.BloqueioDetectado import Bloqueio
load_dotenv()

PASTA_SAIDA = Path(os.getenv("PASTA_SAIDA"))
CAMINHO_BANCO = Path(os.getenv("CAMINHO_BANCO"))
CAMINHO_LOG = Path(os.getenv("CAMINHO_LOG"))
PASTA_DEPURACAO = Path(os.getenv("PASTA_DEPURACAO"))

registro_log = logging.getLogger("motor_de_raspagem")


def configurar_log():
    # 2. Chame uma vez no arquivo principal.
    # Console mostra INFO; o arquivo guarda DEBUG (inclui a URL de cada requisição)
    CAMINHO_LOG.parent.mkdir(parents=True, exist_ok=True)

    formato = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    manipulador_console = logging.StreamHandler()
    manipulador_console.setLevel(logging.INFO)
    manipulador_console.setFormatter(formato)

    manipulador_arquivo = logging.FileHandler(CAMINHO_LOG, encoding="utf-8")
    manipulador_arquivo.setLevel(logging.DEBUG)
    manipulador_arquivo.setFormatter(formato)

    raiz = logging.getLogger()
    raiz.setLevel(logging.DEBUG)
    raiz.addHandler(manipulador_console)
    raiz.addHandler(manipulador_arquivo)

    # 3. Silencia o ruído de conexão do urllib3, mas mantém os avisos de retentativa
    logging.getLogger("urllib3").setLevel(logging.WARNING)




class FiltroDePrecoIgnorado(Exception):
    """Todos os imóveis de uma página vieram fora da faixa pedida."""


URL_LISTAGEM_FAIXA = (
    "https://www.vivareal.com.br/venda/sp/sao-paulo/apartamento_residencial/"
    "?onde=%2CS%C3%A3o+Paulo%2CS%C3%A3o+Paulo%2C%2C%2C%2C%2Ccity%2C"
    "BR%3ESao+Paulo%3ENULL%3ESao+Paulo%2C-23.555771%2C-46.639557%2C"
    "&tipos=apartamento_residencial"
    "&precoMinimo={preco_minimo}&precoMaximo={preco_maximo}&pagina={pagina}"
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
PADRAO_LINK_IMOVEL = re.compile(r'href="([^"]*?/imovel/[^"]*)"')

TIMEOUT_CONEXAO_SEGUNDOS = 10
TIMEOUT_LEITURA_SEGUNDOS = 30

# 4. Limite de páginas que o site entrega por busca
LIMITE_PAGINAS_SITE = 50

# 5. Quantos HTMLs de página vazia guardar por execução (evita encher a pasta
# quando as faixas de preço muito alto vierem todas vazias)
MAXIMO_HTML_DEPURACAO_POR_EXECUCAO = 10

# 5.1. Pausa quando o site responde 429 (Too Many Requests)
PAUSA_APOS_429_SEGUNDOS = 180
MAXIMO_PAUSAS_SEGUIDAS_POR_429 = 3


class MotorDeRaspagem:
    def __init__(
        self,
        caminho_banco=CAMINHO_BANCO,
        passo_faixa_preco=5000, # Varíavel que vai ser utilizada no incremento de valor_faixa_atual + passo_faixa_preco. 
        segundos_espera_minimo=2,
        segundos_espera_maximo=4,
    ):
        self.caminho_banco = Path(caminho_banco)
        self.passo_faixa_preco = passo_faixa_preco
        self.segundos_espera_minimo = segundos_espera_minimo
        self.segundos_espera_maximo = segundos_espera_maximo

        self.ja_fez_requisicao = False
        self.html_depuracao_salvos = 0

        self.sessao_http = self.criar_sessao_http()

        self.caminho_banco.parent.mkdir(parents=True, exist_ok=True)
        self.conexao_banco = sqlite3.connect(self.caminho_banco)
        self.criar_tabelas()

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

        politica_retentativa = Retry(
            total=3,
            backoff_factor=2,
            # 429 fica de fora: quem trata é o buscar_pagina_listagem, com pausa longa
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adaptador = HTTPAdapter(max_retries=politica_retentativa)
        sessao.mount("https://", adaptador)
        sessao.mount("http://", adaptador)
        return sessao

    # ---------- banco de dados ----------

    def criar_tabelas(self):
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

            # 6. Banco antigo não tem a coluna faixa_id: adiciona sem perder dados
            colunas_existentes = {
                linha[1] for linha in self.conexao_banco.execute("PRAGMA table_info(imoveis)")
            }
            if "faixa_id" not in colunas_existentes:
                self.conexao_banco.execute("ALTER TABLE imoveis ADD COLUMN faixa_id INTEGER")

            # 7. Tabela de controle: uma linha por faixa de preço.
            # finalizado = 0 aberta, 1 encerrada. motivo_finalizacao: fim_resultados
            # (acabaram os imóveis da faixa) ou limite_50_paginas (pode estar truncada)
            self.conexao_banco.execute(
                """
                CREATE TABLE IF NOT EXISTS faixas_preco (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preco_minimo INTEGER NOT NULL,
                    preco_maximo INTEGER NOT NULL,
                    segmento_valor TEXT,
                    numero_paginas INTEGER DEFAULT 0,
                    finalizado INTEGER DEFAULT 0,
                    motivo_finalizacao TEXT,
                    imoveis_novos INTEGER DEFAULT 0,
                    observacao TEXT,
                    data_criacao TEXT,
                    data_atualizacao TEXT,
                    UNIQUE (preco_minimo, preco_maximo)
                )
                """
            )

    # ---------- controle de faixas ----------

    def adicionar_faixa(self, preco_minimo, preco_maximo):
        # 8. Pode ser chamado sempre: se a faixa já existe, não faz nada
        if preco_minimo > preco_maximo: # Regra de negócio preco minimo não pode exceder o máximo
            raise ValueError("preco_minimo não pode ser maior que preco_maximo")

        existente = self.conexao_banco.execute(
            "SELECT id FROM faixas_preco WHERE preco_minimo = ? AND preco_maximo = ?",
            (preco_minimo, preco_maximo),
        ).fetchone()
        if existente: # Se Faixa com as ranges já existem <--
            registro_log.info("Faixa %s-%s já existe (id %s)", preco_minimo, preco_maximo, existente[0])
            return existente[0]

        # 9. Recusa faixa que se sobrepõe a outra: duplicaria a coleta de preços
        sobreposta = self.conexao_banco.execute(
            "SELECT preco_minimo, preco_maximo FROM faixas_preco "
            "WHERE preco_minimo <= ? AND preco_maximo >= ? LIMIT 1",
            (preco_maximo, preco_minimo),
        ).fetchone()
        if sobreposta:
            registro_log.error(
                "Faixa %s-%s recusada: sobrepõe a faixa %s-%s",
                preco_minimo, preco_maximo, sobreposta[0], sobreposta[1],
            )
            return None

        agora = datetime.now().isoformat()
        with self.conexao_banco:
            cursor = self.conexao_banco.execute(  # Insere faixas
                """
                INSERT INTO faixas_preco
                (preco_minimo, preco_maximo, segmento_valor, data_criacao, data_atualizacao)
                VALUES (?, ?, ?, ?, ?)
                """,
                (preco_minimo, preco_maximo, f"{preco_minimo}-{preco_maximo}", agora, agora),
            )
        registro_log.info("Faixa %s-%s criada (id %s)", preco_minimo, preco_maximo, cursor.lastrowid)
        return cursor.lastrowid

    def criar_proxima_faixa(self) -> Faixa|None:  # Não Insere nova faixa, apenas monta os paramêtros para a querie. 
        # 10. A próxima começa em "maior preço já cadastrado + 1", sem buracos
        maior_preco_cadastrado = self.conexao_banco.execute(
            "SELECT MAX(preco_maximo) FROM faixas_preco"
        ).fetchone()[0]
        if maior_preco_cadastrado is None: # Caso nenhuma faixa esteja cadastrada no sistema.
            return None 

        novo_minimo = maior_preco_cadastrado + 1
        novo_maximo = maior_preco_cadastrado + self.passo_faixa_preco
        self.adicionar_faixa(novo_minimo, novo_maximo) # <- Insert no database
        return self.obter_proxima_faixa_aberta() # <- Retorna próxima faixa.

    def obter_proxima_faixa_aberta(self) -> Faixa | None: 
        linha = self.conexao_banco.execute(
            """
            SELECT id, preco_minimo, preco_maximo, numero_paginas
            FROM faixas_preco
            WHERE finalizado = 0
            ORDER BY preco_minimo
            LIMIT 1
            """
        ).fetchone()
        if linha is None: # Caso Não exista nennhuma faixa aberta.
            return None
        return {
            "id": linha[0],
            "preco_minimo": linha[1],
            "preco_maximo": linha[2],
            "numero_paginas": linha[3],
        }

    def atualizar_progresso_faixa(self, faixa_id, paginas_concluidas, novos, motivo, observacao):
        # Sem commit: roda na mesma transação dos imóveis da página
        self.conexao_banco.execute(
            """
            UPDATE faixas_preco
            SET numero_paginas = ?,
                imoveis_novos = imoveis_novos + ?,
                finalizado = ?,
                motivo_finalizacao = ?,
                observacao = COALESCE(?, observacao),
                data_atualizacao = ?
            WHERE id = ?
            """,
            (
                paginas_concluidas, novos, 1 if motivo else 0, motivo,
                observacao, datetime.now().isoformat(), faixa_id,
            ),
        )

    def registrar_resumo(self):
        linhas = self.conexao_banco.execute(
            """
            SELECT finalizado, COALESCE(motivo_finalizacao, '-'), COUNT(*), SUM(imoveis_novos)
            FROM faixas_preco
            GROUP BY finalizado, motivo_finalizacao
            """
        ).fetchall()
        total_imoveis = self.conexao_banco.execute("SELECT COUNT(*) FROM imoveis").fetchone()[0]

        registro_log.info("=== Resumo do banco ===")
        for finalizado, motivo, quantidade, soma_novos in linhas:
            registro_log.info(
                "finalizado=%s motivo=%s: %s faixas, %s imóveis novos",
                finalizado, motivo, quantidade, soma_novos or 0,
            )
        registro_log.info("Total de imóveis na tabela: %s", total_imoveis)

    # ---------- coleta ----------

    def esperar_intervalo_entre_requisicoes(self):
        # 11. Espera aleatória antes de toda requisição, menos a primeira da execução
        if self.ja_fez_requisicao:
            time.sleep(random.uniform(self.segundos_espera_minimo, self.segundos_espera_maximo))
        self.ja_fez_requisicao = True

    def buscar_pagina_listagem(self, faixa, numero_pagina):
        url_pagina = URL_LISTAGEM_FAIXA.format(
            preco_minimo=faixa["preco_minimo"],
            preco_maximo=faixa["preco_maximo"],
            pagina=numero_pagina,
        )

        pausas_por_429_nesta_pagina = 0

        while True:
            self.esperar_intervalo_entre_requisicoes()
            registro_log.debug("GET %s", url_pagina)

            inicio = time.monotonic()
            resposta = self.sessao_http.get(
                url_pagina, timeout=(TIMEOUT_CONEXAO_SEGUNDOS, TIMEOUT_LEITURA_SEGUNDOS)
            )
            duracao_ms = int((time.monotonic() - inicio) * 1000)

            registro_log.info(
                "GET faixa %s-%s página %s -> status %s, %s bytes, %s ms",
                faixa["preco_minimo"], faixa["preco_maximo"], numero_pagina,
                resposta.status_code, len(resposta.content), duracao_ms,
            )

            if resposta.status_code != 429:
                break

            # 12.1. 429 = site pedindo calma. Descansa e tenta a MESMA página de novo.
            # Se o site mandar Retry-After maior que a pausa padrão, respeita o dele
            pausas_por_429_nesta_pagina += 1
            if pausas_por_429_nesta_pagina > MAXIMO_PAUSAS_SEGUIDAS_POR_429:
                raise Bloqueio(
                    f"429 persistente: {MAXIMO_PAUSAS_SEGUIDAS_POR_429} pausas seguidas não bastaram "
                    f"na faixa {faixa['preco_minimo']}-{faixa['preco_maximo']} página {numero_pagina}"
                )

            retry_after = resposta.headers.get("Retry-After", "")
            segundos_de_pausa = PAUSA_APOS_429_SEGUNDOS
            if retry_after.isdigit():
                segundos_de_pausa = max(PAUSA_APOS_429_SEGUNDOS, int(retry_after))

            registro_log.warning(
                "429 Too Many Requests (Retry-After=%r). Pausa de %s s e nova tentativa (%s/%s)",
                retry_after, segundos_de_pausa,
                pausas_por_429_nesta_pagina, MAXIMO_PAUSAS_SEGUIDAS_POR_429,
            )
            time.sleep(segundos_de_pausa)

        # 12. 401/403 é bloqueio: não adianta tentar de novo
        if resposta.status_code in (401, 403):
            raise Bloqueio(
                f"Status {resposta.status_code} na faixa {faixa['preco_minimo']}-"
                f"{faixa['preco_maximo']} página {numero_pagina}"
            )

        resposta.raise_for_status()
        return resposta.text

    def extrair_links_imoveis(self, html):
        links_brutos = PADRAO_LINK_IMOVEL.findall(html)
        links_limpos = {link.split("?")[0] for link in links_brutos}
        return sorted(links_limpos)

    def extrair_dados_da_url(self, url_imovel):
        correspondencia = PADRAO_SLUG_IMOVEL.search(url_imovel)
        if correspondencia is None:
            return None

        local_bruto = correspondencia.group("local")

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

    def salvar_imovel(self, dados_imovel, numero_pagina, faixa_id):
        cursor = self.conexao_banco.execute(
            """
            INSERT OR IGNORE INTO imoveis
            (id, url, tipo, quartos, bairro, zona, tem_garagem, area_m2, preco,
             pagina_descoberta, data_coleta, faixa_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dados_imovel["id"], dados_imovel["url"], dados_imovel["tipo"],
                dados_imovel["quartos"], dados_imovel["bairro"], dados_imovel["zona"],
                dados_imovel["tem_garagem"], dados_imovel["area_m2"], dados_imovel["preco"],
                numero_pagina, datetime.now().isoformat(), faixa_id,
            ),
        )
        return cursor.rowcount == 1

    def salvar_html_para_depuracao(self, faixa, numero_pagina, html):
        if self.html_depuracao_salvos >= MAXIMO_HTML_DEPURACAO_POR_EXECUCAO:
            return
        PASTA_DEPURACAO.mkdir(parents=True, exist_ok=True)
        nome_arquivo = (
            f"faixa_{faixa['preco_minimo']}-{faixa['preco_maximo']}_pagina_{numero_pagina}.html"
        )
        caminho_arquivo = PASTA_DEPURACAO / nome_arquivo
        with open(caminho_arquivo, "w", encoding="utf-8") as arquivo:
            arquivo.write(html)
        self.html_depuracao_salvos += 1
        registro_log.warning("HTML salvo para análise em %s", caminho_arquivo)

    def processar_pagina(self, faixa, numero_pagina, links_imoveis):
        # 13. Primeiro extrai e valida tudo, depois grava. Se algo estiver errado
        # (filtro ignorado), levanta erro ANTES de sujar o banco
        dados_validos = []
        sem_padrao = 0

        for url_imovel in links_imoveis:
            dados_imovel = self.extrair_dados_da_url(url_imovel)
            if dados_imovel is None:
                sem_padrao += 1
                registro_log.warning("URL fora do padrão: %s", url_imovel)
                continue
            dados_validos.append(dados_imovel)

        # 14. O preço está no slug: confere se cada imóvel está dentro da faixa pedida.
        # Isso testa o filtro do site (e se as pontas são inclusivas) sem requisição extra
        fora_da_faixa = [
            dados for dados in dados_validos
            if not (faixa["preco_minimo"] <= dados["preco"] <= faixa["preco_maximo"])
        ]
        for dados in fora_da_faixa:
            registro_log.warning(
                "Imóvel %s com preço %s fora da faixa %s-%s",
                dados["id"], dados["preco"], faixa["preco_minimo"], faixa["preco_maximo"],
            )
        if dados_validos and len(fora_da_faixa) == len(dados_validos):
            raise FiltroDePrecoIgnorado(
                f"Todos os {len(dados_validos)} imóveis da página {numero_pagina} estão fora "
                f"da faixa {faixa['preco_minimo']}-{faixa['preco_maximo']}"
            )

        # 15. Decide se a faixa termina nesta página
        observacao = None
        if not links_imoveis:
            motivo = "fim_resultados"
            paginas_concluidas = numero_pagina - 1
            if paginas_concluidas == 0:
                observacao = "faixa sem imóveis (página 1 vazia)"
        elif numero_pagina >= LIMITE_PAGINAS_SITE:
            motivo = "limite_50_paginas"
            paginas_concluidas = numero_pagina
            observacao = "possível truncamento: dividir em faixas menores"
        else:
            motivo = None
            paginas_concluidas = numero_pagina

        # 16. Imóveis + progresso + encerramento da faixa numa transação só.
        # Ctrl+C no meio faz rollback: a página inteira é gravada ou nenhuma
        novos = 0
        with self.conexao_banco:
            for dados_imovel in dados_validos:
                if self.salvar_imovel(dados_imovel, numero_pagina, faixa["id"]):
                    novos += 1
            self.atualizar_progresso_faixa(faixa["id"], paginas_concluidas, novos, motivo, observacao)

        return {
            "links": len(links_imoveis),
            "novos": novos,
            "repetidos": len(dados_validos) - novos,
            "sem_padrao": sem_padrao,
            "fora_da_faixa": len(fora_da_faixa),
            "motivo": motivo,
            "paginas_concluidas": paginas_concluidas,
        }

    def raspar_faixa(self, faixa):
        pagina_inicial = faixa["numero_paginas"] + 1
        registro_log.info(
            "Faixa %s-%s (id %s): começando na página %s",
            faixa["preco_minimo"], faixa["preco_maximo"], faixa["id"], pagina_inicial,
        )

        # Defesa: faixa aberta já na página 50 (não deveria ocorrer, a transação é única)
        if pagina_inicial > LIMITE_PAGINAS_SITE: # <- Regra de interrupção da faixa.
            with self.conexao_banco:
                self.atualizar_progresso_faixa(
                    faixa["id"], LIMITE_PAGINAS_SITE, 0, "limite_50_paginas",
                    "encerrada na retomada: já estava no limite",
                )
            return {"motivo": "limite_50_paginas", "paginas": LIMITE_PAGINAS_SITE, "novos": 0, "vazia": False}

        total_novos = 0

        for numero_pagina in range(pagina_inicial, LIMITE_PAGINAS_SITE + 1):
            html = self.buscar_pagina_listagem(faixa, numero_pagina)
            links_imoveis = self.extrair_links_imoveis(html)

            # 17. Página sem links: registra o tamanho do HTML (uma página de bloqueio
            # costuma ter tamanho diferente de uma lista realmente vazia) e guarda uma cópia
            if not links_imoveis:
                registro_log.warning(
                    "Faixa %s-%s página %s sem links de imóvel (HTML com %s caracteres)",
                    faixa["preco_minimo"], faixa["preco_maximo"], numero_pagina, len(html),
                )
                self.salvar_html_para_depuracao(faixa, numero_pagina, html)

            resultado = self.processar_pagina(faixa, numero_pagina, links_imoveis)
            total_novos += resultado["novos"]

            registro_log.info(
                "Página %s: %s links, %s novos, %s repetidos, %s fora do padrão, %s fora da faixa",
                numero_pagina, resultado["links"], resultado["novos"], resultado["repetidos"],
                resultado["sem_padrao"], resultado["fora_da_faixa"],
            )

            if resultado["motivo"]:
                registro_log.info(
                    "Faixa %s-%s finalizada (%s): %s páginas, %s imóveis novos nesta execução",
                    faixa["preco_minimo"], faixa["preco_maximo"], resultado["motivo"],
                    resultado["paginas_concluidas"], total_novos,
                )
                return {
                    "motivo": resultado["motivo"],
                    "paginas": resultado["paginas_concluidas"],
                    "novos": total_novos,
                    "vazia": resultado["paginas_concluidas"] == 0,
                }

    def executar(self, maximo_faixas_vazias_seguidas=None):
        # 18. Roda faixa após faixa. Só para por: Ctrl+C, bloqueio, erro de rede,
        # filtro ignorado, erro inesperado, ou (se você definir) faixas vazias demais
        registro_log.info(
            "Execução iniciada | banco=%s | passo=%s | espera=%s-%ss | limite_vazias=%s",
            self.caminho_banco, self.passo_faixa_preco,
            self.segundos_espera_minimo, self.segundos_espera_maximo,
            maximo_faixas_vazias_seguidas,
        )

        faixa_atual = None
        faixas_vazias_seguidas = 0
        faixas_finalizadas_nesta_execucao = 0

        try:
            while True:
                faixa_atual = self.obter_proxima_faixa_aberta() # Verificar Se existe faixa Aberta, Se não tiver = none
                if faixa_atual is None:
                    faixa_atual = self.criar_proxima_faixa() # Criar nova faixa.
                if faixa_atual is None:  # É preciso pelo menoss 1 faixa no data.base
                    registro_log.error(
                        "Tabela faixas_preco vazia. Cadastre a primeira faixa, "
                        "por exemplo motor.adicionar_faixa(100000, 105000)."
                    )
                    break

                resultado = self.raspar_faixa(faixa_atual)
                faixas_finalizadas_nesta_execucao += 1

                if resultado["vazia"]:
                    faixas_vazias_seguidas += 1
                else:
                    faixas_vazias_seguidas = 0

                if (
                    maximo_faixas_vazias_seguidas is not None
                    and faixas_vazias_seguidas >= maximo_faixas_vazias_seguidas
                ):
                    registro_log.warning(
                        "%s faixas vazias seguidas: parando (limite definido por você)",
                        faixas_vazias_seguidas,
                    )
                    break

        except KeyboardInterrupt:
            registro_log.warning("Interrompido pelo usuário (Ctrl+C). %s", self.descrever_retomada(faixa_atual))
        except Bloqueio as erro:
            registro_log.error("BLOQUEIO DETECTADO: %s. %s", erro, self.descrever_retomada(faixa_atual))
        except FiltroDePrecoIgnorado as erro:
            registro_log.error("FILTRO DE PREÇO IGNORADO: %s. %s", erro, self.descrever_retomada(faixa_atual))
        except requests.RequestException:
            registro_log.exception("Falha de rede. %s", self.descrever_retomada(faixa_atual))
        except Exception:
            registro_log.exception("Erro inesperado. %s", self.descrever_retomada(faixa_atual))
        finally:
            registro_log.info(
                "Execução encerrada: %s faixas finalizadas nesta execução",
                faixas_finalizadas_nesta_execucao,
            )
            self.registrar_resumo()

    def descrever_retomada(self, faixa_atual):
        if faixa_atual is None:
            return "Nenhuma faixa em andamento."
        faixa_no_banco = self.conexao_banco.execute(
            "SELECT numero_paginas, finalizado FROM faixas_preco WHERE id = ?", (faixa_atual["id"],)
        ).fetchone()
        paginas_concluidas, finalizado = faixa_no_banco
        if finalizado:
            return f"Faixa {faixa_atual['preco_minimo']}-{faixa_atual['preco_maximo']} já estava finalizada."
        return (
            f"Retoma na faixa {faixa_atual['preco_minimo']}-{faixa_atual['preco_maximo']}, "
            f"página {paginas_concluidas + 1}."
        )