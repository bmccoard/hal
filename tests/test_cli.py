import io

from neo.cli import main


class CP1252Buffer(io.StringIO):
    def write(self, value: str) -> int:
        value.encode("cp1252")
        return super().write(value)


def test_help_is_windows_console_safe() -> None:
    output = CP1252Buffer()
    assert main(["help"], stdout=output) == 0
    assert "Interactive chat mode" in output.getvalue()


def test_unknown_command_returns_usage_error() -> None:
    output, error = io.StringIO(), io.StringIO()
    assert main(["wat"], stdout=output, stderr=error) == 2
    assert "unknown command: wat" in error.getvalue()
