import pytest

from model.client.ClienteBanco import ClientDatabase
from model.RawImoveis.OperationRawImoveis import Repositorio_OperationRawImoveis
from model.RawImoveis.RawImoveis import COLUNAS_RAW_IMOVEIS, Repositorio_RawImoveis


@pytest.fixture
def cliente():
    cliente = ClientDatabase(":memory:")
    cliente.executar_query("CREATE TABLE imoveis (id TEXT PRIMARY KEY, url TEXT)")
    yield cliente
    cliente.conexao.close()


@pytest.fixture
def raw_repo(cliente):
    repositorio = Repositorio_RawImoveis(cliente)
    repositorio.criar_tabela()
    return repositorio


@pytest.fixture
def operacao_repo(cliente):
    repositorio = Repositorio_OperationRawImoveis(cliente)
    repositorio.criar_tabela()
    return repositorio


def montar_dados(id_anuncio="1"):
    dados = {coluna: None for coluna in COLUNAS_RAW_IMOVEIS}
    dados.update(id_anuncio=id_anuncio, preco=200000, bairro="Liberdade")
    return dados


def test_colunas_da_tabela_batem_com_a_lista_de_colunas(cliente, raw_repo):
    colunas_tabela = [linha[1] for linha in cliente.executar_query("PRAGMA table_info(raw_imoveis)")]
    assert colunas_tabela == ["id", *COLUNAS_RAW_IMOVEIS]


def test_adicionar_imovel_e_ler_de_volta(cliente, raw_repo):
    with cliente.conexao:
        assert raw_repo.adicionar_imovel(montar_dados("10")) is True
    lido = raw_repo.obter_por_id_anuncio("10")
    assert lido["preco"] == 200000 and lido["bairro"] == "Liberdade"


def test_mesmo_id_anuncio_nao_duplica(cliente, raw_repo):
    with cliente.conexao:
        assert raw_repo.adicionar_imovel(montar_dados("10")) is True
        assert raw_repo.adicionar_imovel(montar_dados("10")) is False
    assert raw_repo.contar_imoveis() == 1


def test_registrar_resultado_atualiza_em_vez_de_duplicar(cliente, operacao_repo):
    with cliente.conexao:
        operacao_repo.registrar_resultado("10", 0, "erro_rede: Timeout")
        operacao_repo.registrar_resultado("10", 1, None)
    linhas = cliente.executar_query("SELECT imovel_id, finalizado, motivo FROM operation_raw_imoveis").fetchall()
    assert linhas == [("10", 1, None)]


def test_pendentes_sao_os_sem_operacao_ou_abertos(cliente, operacao_repo):
    with cliente.conexao:
        for numero in ("1", "2", "3"):
            cliente.executar_query("INSERT INTO imoveis (id, url) VALUES (?, ?)", (numero, f"url-{numero}"))
        operacao_repo.registrar_resultado("1", 1, None)               # finalizado: sai da fila
        operacao_repo.registrar_resultado("2", 0, "erro_rede: X")     # aberto: continua na fila
    pendentes = operacao_repo.obter_pendentes(0, 10)
    assert [(imovel_id, url) for _rowid, imovel_id, url in pendentes] == [("2", "url-2"), ("3", "url-3")]


def test_pendentes_respeitam_rowid_inicial_e_limite(cliente, operacao_repo):
    with cliente.conexao:
        for numero in ("1", "2", "3"):
            cliente.executar_query("INSERT INTO imoveis (id, url) VALUES (?, ?)", (numero, f"url-{numero}"))
    primeiro_lote = operacao_repo.obter_pendentes(0, 2)
    assert [linha[1] for linha in primeiro_lote] == ["1", "2"]
    segundo_lote = operacao_repo.obter_pendentes(primeiro_lote[-1][0], 2)
    assert [linha[1] for linha in segundo_lote] == ["3"]