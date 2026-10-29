# 1. Imports agrupados na ordem do PEP 8: biblioteca padrão, terceiros, projeto
import logging
import re
from collections import Counter
from pathlib import Path

import requests

from engine.ClientHttp import ClienteHttp
from engine.Configuracao import ler_caminho_do_ambiente
from engine.erros.BloqueioDetectado import Bloqueio
from engine.erros.LayoutAlterado import LayoutAlterado
from engine.extratores.ExtratorImovelVivaReal import extrair_dados_do_html, listar_campos_faltando
from model.client.ClienteBanco import ClientDatabase
from model.RawImoveis.OperationRawImoveis import Repositorio_OperationRawImoveis
from model.RawImoveis.RawImoveis import Repositorio_RawImoveis

registro_log = logging.getLogger("motor_de_raspagem")

# O id do imóvel está no final do slug: .../apartamento-1-quartos-...-venda-RS200000-id-2916943833/
PADRAO_ID_URL = re.compile(r"-id-(\d+)")

# 2. Quantos HTMLs de layout alterado guardar por execução (evita encher a pasta)
MAXIMO_HTML_DEPURACAO_POR_EXECUCAO = 10

# 3. Resultados possíveis de raspar_imovel (as strings também aparecem no resumo do log)
RESULTADO_OK = "ok"
RESULTADO_DADOS_INCOMPLETOS = "dados_incompletos"
RESULTADO_INATIVO = "anuncio_inativo"
RESULTADO_INDISPONIVEL = "anuncio_indisponivel"
RESULTADO_ERRO_REDE = "erro_rede"
RESULTADO_URL_INVALIDA = "url_invalida"


class MotorDeRaspagemImoveis:
    def __init__(
        self,
        caminho_banco=None,
        pasta_depuracao=None,
        segundos_espera_minimo=2,
        segundos_espera_maximo=4,
    ):
        # 4. Sem caminho informado, lê do .env (CAMINHO_BANCO). A pasta de depuração só é lida se precisar
        self.caminho_banco = Path(caminho_banco or ler_caminho_do_ambiente("CAMINHO_BANCO"))
        self.pasta_depuracao = pasta_depuracao
        self.html_depuracao_salvos = 0

        self.caminho_banco.parent.mkdir(parents=True, exist_ok=True)
        self.conexao_banco = ClientDatabase(self.caminho_banco)
        self.raw_repo: Repositorio_RawImoveis = Repositorio_RawImoveis(self.conexao_banco)
        self.operacao_repo: Repositorio_OperationRawImoveis = Repositorio_OperationRawImoveis(self.conexao_banco)
        self.cliente_http = ClienteHttp(segundos_espera_minimo, segundos_espera_maximo)

        self.raw_repo.criar_tabela()
        self.operacao_repo.criar_tabela()

    def __enter__(self):
        return self

    def __exit__(self, tipo_excecao, valor_excecao, rastreio):
        self.fechar()
        return False

    def fechar(self):
        self.cliente_http.fechar()
        self.conexao_banco.conexao.close()

    # ---------- coleta de um imóvel ----------

    def raspar_imovel(self, url_imovel):
        # 5. A função recebe só a URL: o id do imóvel vem do próprio slug
        correspondencia = PADRAO_ID_URL.search(url_imovel)
        if correspondencia is None:
            registro_log.warning("URL sem id, ignorada: %s", url_imovel)
            return RESULTADO_URL_INVALIDA
        imovel_id = correspondencia.group(1)

        try:
            resposta = self.cliente_http.buscar(url_imovel)  # Bloqueio sobe direto para o executar
            # 6. Anúncio removido: o site responde 404/410 ou redireciona para fora de /imovel/
            if resposta.status_code in (404, 410) or "/imovel/" not in resposta.url:
                self.registrar_sem_dados(imovel_id, finalizado=1, motivo="anuncio_indisponivel")
                return RESULTADO_INDISPONIVEL
            resposta.raise_for_status()
        except requests.RequestException as erro:
            # 7. Erro de rede/HTTP: deixa a operação ABERTA para tentar de novo em outra execução
            self.registrar_sem_dados(imovel_id, finalizado=0, motivo=f"erro_rede: {type(erro).__name__}")
            registro_log.warning("Imóvel %s: %s", imovel_id, erro)
            return RESULTADO_ERRO_REDE

        try:
            dados_imovel = extrair_dados_do_html(resposta.text, url_imovel)
        except LayoutAlterado:
            self.salvar_html_para_depuracao(imovel_id, resposta.text)
            raise

        # 8. Decide o motivo: o raw é gravado nos três casos, o motivo só explica a situação
        campos_faltando = listar_campos_faltando(dados_imovel)
        status_anuncio = dados_imovel["status_anuncio"]
        motivo = None
        resultado = RESULTADO_OK
        if campos_faltando:
            motivo = f"dados_incompletos: {','.join(campos_faltando)}"
            resultado = RESULTADO_DADOS_INCOMPLETOS
        elif status_anuncio not in (None, "ACTIVE"):
            motivo = f"anuncio_inativo: {status_anuncio}"
            resultado = RESULTADO_INATIVO

        # 9. Raw + operação numa transação só: grava os dois ou nenhum
        with self.conexao_banco.conexao:
            self.raw_repo.adicionar_imovel(dados_imovel)
            self.operacao_repo.registrar_resultado(imovel_id, 1, motivo)

        registro_log.info("Imóvel %s -> %s%s", imovel_id, resultado, f" ({motivo})" if motivo else "")
        return resultado

    def registrar_sem_dados(self, imovel_id, finalizado, motivo):
        with self.conexao_banco.conexao:
            self.operacao_repo.registrar_resultado(imovel_id, finalizado, motivo)
        registro_log.info("Imóvel %s -> %s", imovel_id, motivo)

    def salvar_html_para_depuracao(self, imovel_id, html):
        if self.html_depuracao_salvos >= MAXIMO_HTML_DEPURACAO_POR_EXECUCAO:
            return
        pasta = Path(self.pasta_depuracao or ler_caminho_do_ambiente("PASTA_DEPURACAO"))
        pasta.mkdir(parents=True, exist_ok=True)
        caminho_arquivo = pasta / f"layout_alterado_imovel_{imovel_id}.html"
        caminho_arquivo.write_text(html, encoding="utf-8")
        self.html_depuracao_salvos += 1
        registro_log.warning("HTML salvo para análise em %s", caminho_arquivo)

    # ---------- execução ----------

    def executar(self, tamanho_lote=100, maximo_erros_rede_seguidos=5):
        # 10. Processa os imóveis pendentes. Só para por: Ctrl+C, bloqueio, layout alterado,
        # erro inesperado, acabar a fila, ou vários erros de rede seguidos (site fora do ar)
        registro_log.info("Execução iniciada | banco=%s | lote=%s", self.caminho_banco, tamanho_lote)

        contagem_resultados = Counter()
        ultimo_rowid = 0
        erros_rede_seguidos = 0

        try:
            while True:
                lote = self.operacao_repo.obter_pendentes(ultimo_rowid, tamanho_lote)
                if not lote:
                    registro_log.info("Fila vazia: nenhum imóvel pendente.")
                    break

                for rowid, _imovel_id, url_imovel in lote:
                    ultimo_rowid = rowid
                    resultado = self.raspar_imovel(url_imovel)
                    contagem_resultados[resultado] += 1

                    if resultado == RESULTADO_ERRO_REDE:
                        erros_rede_seguidos += 1
                    else:
                        erros_rede_seguidos = 0

                    if erros_rede_seguidos >= maximo_erros_rede_seguidos:
                        registro_log.error(
                            "%s erros de rede seguidos: parando. Rode de novo mais tarde, retoma de onde parou.",
                            erros_rede_seguidos,
                        )
                        return

        except KeyboardInterrupt:
            registro_log.warning("Interrompido pelo usuário (Ctrl+C). Retoma de onde parou na próxima execução.")
        except Bloqueio as erro:
            registro_log.error("BLOQUEIO DETECTADO: %s. Retoma de onde parou na próxima execução.", erro)
        except LayoutAlterado as erro:
            registro_log.error(
                "LAYOUT ALTERADO (ou página de bloqueio com status 200): %s. HTML salvo na pasta de depuração.", erro
            )
        except Exception:
            registro_log.exception("Erro inesperado.")
        finally:
            self.registrar_resumo(contagem_resultados)

    def registrar_resumo(self, contagem_resultados):
        registro_log.info("=== Resumo desta execução ===")
        for resultado, quantidade in contagem_resultados.most_common():
            registro_log.info("%s: %s", resultado, quantidade)
        registro_log.info("Total de imóveis na raw_imoveis: %s", self.raw_repo.contar_imoveis())
        for finalizado, motivo, quantidade in self.operacao_repo.obter_resumo_por_motivo():
            registro_log.info("operação finalizado=%s motivo=%s: %s", finalizado, motivo, quantidade)