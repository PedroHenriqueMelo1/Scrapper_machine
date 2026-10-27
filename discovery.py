import scrapy


class DiscoverySpider(scrapy.Spider):
    name = "discovery"

    allowed_domains = ["vivareal.com.br"]

    start_urls = [
        "https://www.vivareal.com.br/"
    ]

    # 1. Coloca os teus cookies essenciais aqui nos custom_settings
    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 2,
        "CONCURRENT_REQUESTS": 2,
        "AUTOTHROTTLE_ENABLED": True,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.discovered_urls = set()

    # Opcional: Se preferires injetar via start_requests com dicionário nativo do Scrapy
    def start_requests(self):


        for url in self.start_urls:
            yield scrapy.Request(
                url=url, callback=self.parse
            )

    def parse(self, response):
        links = response.css('a[href*="/imovel/"]::attr(href)').getall()

        for link in links:
            url = response.urljoin(link)

            if url in self.discovered_urls:
                continue

            self.discovered_urls.add(url)

            yield {
                "url": url,
                "discovered_from": response.url,
            }

        next_page = response.css("a.next::attr(href)").get()

        if next_page:
            yield response.follow(
                next_page,
                callback=self.parse,
            )