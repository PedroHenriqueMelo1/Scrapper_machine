import json
import re
from datetime import datetime

from engine.erros.LayoutAlterado import LayoutAlterado

# 1. O Next.js embute os dados da página em chamadas self.__next_f.push([1,"..."]).
# O conteúdo é uma string JS com aspas escapadas, por isso o padrão aceita \" dentro dela
PADRAO_PUSH_NEXT = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
PADRAO_JSON_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
PADRAO_HASH_FOTO = re.compile(r"/vr-listing/([0-9a-f]{32})/")

MARCADOR_DADOS = '"baseData":'

# 2. Sem esses campos o registro não serve para análise: vira "dados_incompletos" no controle.
# Quartos e banheiros ficam de fora porque terreno e sala comercial podem não ter
CAMPOS_OBRIGATORIOS = ("preco", "area_m2", "bairro", "cidade", "tipo_imovel", "operacao")


def obter(bloco, *chaves):
    # Navega em dicionários aninhados sem estourar quando uma chave não existe
    for chave in chaves:
        if not isinstance(bloco, dict):
            return None
        bloco = bloco.get(chave)
    return bloco


def primeiro_valor(lista):
    # O site entrega área/quartos/banheiros como lista (empreendimentos têm vários valores)
    if isinstance(lista, list) and lista:
        return lista[0]
    return None


def extrair_json_embutido(html):
    # 3. Junta os pedaços (um dado pode ser quebrado em vários push) e desfaz o escape da string JS
    pedacos = [json.loads(f'"{pedaco}"') for pedaco in PADRAO_PUSH_NEXT.findall(html)]
    texto = "".join(pedacos)

    posicao = texto.find(MARCADOR_DADOS)
    if posicao == -1:
        raise LayoutAlterado("Marcador baseData não encontrado no HTML")

    inicio = texto.index("{", posicao + len(MARCADOR_DADOS))
    try:
        dados_base, _ = json.JSONDecoder().raw_decode(texto, inicio)
    except json.JSONDecodeError as erro:
        raise LayoutAlterado(f"baseData encontrado, mas o JSON não pôde ser lido: {erro}") from erro
    return dados_base


def extrair_descricao(html):
    # 4. No JSON do Next a descrição vem só como referência ("$29"); o texto completo está no JSON-LD
    for bloco in PADRAO_JSON_LD.findall(html):
        try:
            conteudo = json.loads(bloco)
        except json.JSONDecodeError:
            continue
        if isinstance(conteudo, dict) and conteudo.get("@type") == "Product":
            return conteudo.get("description")
    return None


def extrair_preco_condominio_iptu(precos, operacao):
    # 5. Venda: prices.sale. Aluguel: prices.rental (estrutura do aluguel ainda não
    # validada com HTML real; se os nomes forem outros, o preço vem None e cai em dados_incompletos)
    bloco = obter(precos, "sale" if operacao == "SALE" else "rental") or {}
    return bloco.get("value"), bloco.get("condominium"), bloco.get("iptu")


def extrair_hashes_fotos(imagens):
    # Guarda só o hash de 32 caracteres de cada foto: a URL completa se reconstrói depois
    hashes = []
    for imagem in imagens or []:
        correspondencia = PADRAO_HASH_FOTO.search(imagem.get("dangerousSrc", ""))
        if correspondencia:
            hashes.append(correspondencia.group(1))
    return hashes


def extrair_dados_do_html(html, url_imovel):
    dados_base = extrair_json_embutido(html)

    pagina = obter(dados_base, "pageData")
    listing = obter(pagina, "listing")
    if not listing:
        raise LayoutAlterado("pageData.listing não encontrado no JSON embutido")

    amenities = listing.get("amenities") or {}
    endereco = listing.get("address") or {}
    operacao = listing.get("business")
    preco, valor_condominio, valor_iptu = extrair_preco_condominio_iptu(listing.get("prices"), operacao)
    caracteristicas = amenities.get("values") or []
    hashes_fotos = extrair_hashes_fotos(obter(listing, "medias", "images"))

    # 6. Telefone e WhatsApp existem no JSON, mas NÃO são lidos aqui (LGPD)
    return {
        "id_anuncio": listing.get("id"),
        "codigo_anunciante": listing.get("externalId"),
        "id_anunciante": obter(listing, "advertiser", "legacyId"),
        "tipo_imovel": listing.get("unitType"),
        "operacao": operacao,
        "preco": preco,
        "valor_condominio": valor_condominio,
        "valor_iptu": valor_iptu,
        "area_m2": primeiro_valor(amenities.get("usableAreas")),
        "quartos": primeiro_valor(amenities.get("bedrooms")),
        "banheiros": primeiro_valor(amenities.get("bathrooms")),
        "suites": primeiro_valor(amenities.get("suites")),
        "vagas": primeiro_valor(amenities.get("parkingSpaces")),
        "rua": endereco.get("street"),
        "numero": endereco.get("streetNumber"),
        "bairro": endereco.get("neighborhood"),
        "cidade": endereco.get("city"),
        "estado": endereco.get("stateAcronym"),
        "cep": obter(pagina, "adTrackingMetadata", "property", "location", "address_postal_code"),
        "latitude": obter(endereco, "coordinates", "latitude"),
        "longitude": obter(endereco, "coordinates", "longitude"),
        "caracteristicas": ",".join(caracteristicas) if caracteristicas else None,
        "fotos": ",".join(hashes_fotos) if hashes_fotos else None,
        "descricao": extrair_descricao(html),
        "url": url_imovel,
        "status_anuncio": obter(pagina, "statusOptions", "status"),
        "data_publicacao": pagina.get("createdAt"),
        "data_atualizacao_site": pagina.get("updatedAt"),
        "data_coleta": datetime.now().isoformat(),
    }


def listar_campos_faltando(dados):
    return [campo for campo in CAMPOS_OBRIGATORIOS if dados.get(campo) in (None, "")]