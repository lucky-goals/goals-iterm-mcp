"""Fixed error codes shared by iTerm reads and MCP tools."""


class DomainError(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)
