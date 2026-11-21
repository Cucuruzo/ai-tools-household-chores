import pytest
from django.test import Client


@pytest.mark.django_db
def test_home_redirects_to_dashboard():
    client = Client()
    response = client.get("/")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/"
