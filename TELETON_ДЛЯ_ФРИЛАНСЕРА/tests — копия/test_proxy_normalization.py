"""Тесты нормализации прокси-строк в sender.TelegramSender."""
import pytest
from sender import TelegramSender


_normalize = TelegramSender._normalize_proxy_url
_parse = TelegramSender._parse_proxy


class TestNormalizeProxyUrl:
    def test_canonical_with_scheme_and_at(self):
        """scheme://user:pass@host:port — уже канонический, не трогаем."""
        url = "socks5://user:pwd@host.com:1080"
        assert _normalize(url) == url

    def test_compact_auth_first_with_scheme(self):
        """socks5://user:pass:host:port → канонический."""
        url = "socks5://user:pwd:host.com:1080"
        assert _normalize(url) == "socks5://user:pwd@host.com:1080"

    def test_compact_host_first_with_scheme(self):
        """socks5://host:port:user:pass → канонический."""
        url = "socks5://host.com:1080:user:pwd"
        assert _normalize(url) == "socks5://user:pwd@host.com:1080"

    def test_auth_first_no_scheme(self):
        """user:pass:host:port → socks5://..."""
        url = "user:pwd:host.com:1080"
        assert _normalize(url) == "socks5://user:pwd@host.com:1080"

    def test_host_first_no_scheme(self):
        """host:port:user:pass → socks5://..."""
        url = "host.com:1080:user:pwd"
        assert _normalize(url) == "socks5://user:pwd@host.com:1080"

    def test_http_scheme_preserved(self):
        url = "http://u:p:host.com:8080"
        assert _normalize(url) == "http://u:p@host.com:8080"

    def test_socks4_scheme_preserved(self):
        url = "socks4://u:p:host.com:1080"
        assert _normalize(url) == "socks4://u:p@host.com:1080"

    def test_strips_whitespace(self):
        url = "   user:pwd:host.com:1080   "
        assert _normalize(url) == "socks5://user:pwd@host.com:1080"

    def test_port_edge_values(self):
        """Порт 1 и 65535 — валидны."""
        assert _normalize("u:p:h:1") == "socks5://u:p@h:1"
        assert _normalize("u:p:h:65535") == "socks5://u:p@h:65535"

    def test_wrong_segment_count_raises(self):
        with pytest.raises(ValueError, match="4 сегмент"):
            _normalize("host:1080:user")  # только 3

    def test_no_port_raises(self):
        """Если ни в позиции 2, ни в позиции 4 нет валидного порта."""
        with pytest.raises(ValueError, match=r"(?i)не могу определить порт|4 сегмент"):
            _normalize("aaa:bbb:ccc:ddd")

    def test_port_out_of_range_raises(self):
        """Порт 0 или >65535 невалиден."""
        with pytest.raises(ValueError):
            _normalize("u:p:h:99999")


class TestParseProxy:
    def test_socks5_default(self):
        """socks5 = proxy_type 2."""
        result = _parse("u:p:host.com:1080")
        assert result[0] == 2
        assert result[1] == "host.com"
        assert result[2] == 1080
        assert result[4] == "u"
        assert result[5] == "p"

    def test_socks4_type_1(self):
        result = _parse("socks4://u:p:host.com:1080")
        assert result[0] == 1

    def test_http_type_3(self):
        result = _parse("http://u:p:host.com:8080")
        assert result[0] == 3
