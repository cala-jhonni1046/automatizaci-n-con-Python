import pytest

from jarvis import seguridad


@pytest.fixture(autouse=True)
def restaurar_confirmador():
    """Cada test puede cambiar el confirmador global; al terminar se deja el original."""
    original = seguridad._confirmar
    yield
    seguridad.registrar_confirmador(original)
