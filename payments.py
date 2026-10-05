"""Payment provider registry. Cash works now; online providers are stubs — implement
initiate()/verify() and enable Setting 'online_payment'=1."""


class Provider:
    name = "cash"

    def initiate(self, order):
        return {"type": "counter"}

    def verify(self, payload):
        raise NotImplementedError


class ESewa(Provider):    name = "esewa"
class Khalti(Provider):   name = "khalti"
class Fonepay(Provider):  name = "fonepay"
class Card(Provider):     name = "card"


PROVIDERS = {p.name: p() for p in (Provider, ESewa, Khalti, Fonepay, Card)}