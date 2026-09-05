"""Shared requirements for independent parser comparisons in the platform matrix."""
import pytest


@pytest.fixture
def current_oracle():
    etree = pytest.importorskip('lxml.etree')
    if etree.LIBXML_VERSION < (2, 14):
        pytest.skip(f'oracle libxml2 is {etree.LIBXML_VERSION}; value parity requires >= 2.14')
