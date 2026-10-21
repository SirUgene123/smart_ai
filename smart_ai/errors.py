class RouterError(RuntimeError):
    """An actionable provider or request-routing error."""


class ModelUnavailableError(RouterError):
    """Gemini rejected the selected model for temporary availability or quota reasons."""

    def __init__(self, model: str, message: str, *, requires_confirmation: bool = False) -> None:
        super().__init__(message)
        self.model = model
        self.requires_confirmation = requires_confirmation
