import asyncio
import shutil
import tempfile
from decimal import Decimal

from electrum import exchange_rate
from electrum.exchange_rate import CoinPaprika, ExchangeBase, FxThread, DEFAULT_EXCHANGE
from electrum.simple_config import SimpleConfig

from . import ElectrumTestCase


# One real answer from /v1/tickers/doi-doichain?quotes=EUR, trimmed to the keys
# the code reads. The endpoint returns one entry per requested quote currency.
TICKER_EUR = {
    "id": "doi-doichain",
    "name": "Doichain",
    "symbol": "DOI",
    "quotes": {"EUR": {"price": 0.024258034851817194, "volume_24h": 7.74}},
}


class FakeResponse(CoinPaprika):
    """CoinPaprika with the network taken out: get_json answers from a script."""

    def __init__(self, answers):
        ExchangeBase.__init__(self, lambda: None, lambda: None)
        self.answers = answers
        self.asked = []

    async def get_json(self, site, get_string):
        self.asked.append(get_string)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Test_CoinPaprika(ElectrumTestCase):

    def test_get_rates_returns_a_mapping(self):
        ex = FakeResponse([TICKER_EUR])
        rates = asyncio.get_event_loop().run_until_complete(ex.get_rates('EUR'))
        self.assertIsInstance(rates, dict)
        self.assertEqual({'EUR'}, set(rates))
        self.assertIsInstance(rates['EUR'], Decimal)
        self.assertEqual(Decimal('0.024258034851817194'), rates['EUR'])

    def test_the_selected_currency_is_what_is_asked_for(self):
        # #17: the old code asked for ?quote=usd whatever the user had picked,
        # so a EUR label sat above a USD number.
        ex = FakeResponse([TICKER_EUR])
        asyncio.get_event_loop().run_until_complete(ex.get_rates('EUR'))
        self.assertIn('quotes=EUR', ex.asked[0])
        self.assertNotIn('usd', ex.asked[0].lower())

    def test_a_currency_the_source_does_not_serve_yields_no_rate(self):
        ex = FakeResponse([{"id": "doi-doichain", "quotes": {}}])
        rates = asyncio.get_event_loop().run_until_complete(ex.get_rates('AED'))
        self.assertEqual({}, rates)

    def test_no_history_is_offered(self):
        # #18: the free plan answers every historical range with HTTP 402.
        self.assertEqual([], CoinPaprika(None, None).history_ccys())

    def test_quotes_stay_a_mapping_even_when_get_rates_misbehaves(self):
        # #16: a subclass returning a bare price put a float where a mapping was
        # expected, and an empty dict on failure; every amount the wallet tried
        # to render then raised TypeError.
        ex = FakeResponse([])
        ex.get_rates = lambda ccy: _coro(Decimal('0.0278'))
        asyncio.get_event_loop().run_until_complete(ex.update_safe('EUR'))
        self.assertIsInstance(ex.quotes, dict)
        self.assertEqual({}, ex.quotes)


async def _coro(value):
    return value


class Test_FxThread(ElectrumTestCase):

    def setUp(self):
        super(Test_FxThread, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.config = SimpleConfig({'electrum_path': self.dir})

    def tearDown(self):
        super(Test_FxThread, self).tearDown()
        shutil.rmtree(self.dir)

    def test_missing_rate_is_not_a_number_not_an_exception(self):
        fx = FxThread(self.config, None)
        fx.set_enabled(True)
        fx.exchange.quotes = {}
        self.assertTrue(fx.exchange_rate().is_nan())
        self.assertEqual('', fx.format_amount(100000000))

    def test_rate_is_read_under_the_selected_currency(self):
        fx = FxThread(self.config, None)
        fx.set_enabled(True)
        fx.ccy = 'EUR'
        fx.exchange.quotes = {'EUR': Decimal('0.02'), 'USD': Decimal('0.0278')}
        self.assertEqual(Decimal('0.02'), fx.exchange_rate())

    def test_a_source_that_prices_bitcoin_is_not_kept(self):
        # #24: a wallet configured before those sources were withdrawn carries
        # one in its config file.
        self.config.set_key('use_exchange', 'Kraken')
        fx = FxThread(self.config, None)
        self.assertEqual(DEFAULT_EXCHANGE, fx.config_exchange())
        self.assertEqual(DEFAULT_EXCHANGE, fx.exchange.name())

    def test_only_doi_sources_are_offered(self):
        for name in exchange_rate.CURRENCIES:
            self.assertTrue(globals().get(name, getattr(exchange_rate, name)).quotes_doi,
                            msg=f"{name} is offered but does not quote DOI")
