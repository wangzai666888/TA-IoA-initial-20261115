class CapabilityCatalog:
    """Candidate retrieval is separate from exact capability verification."""

    def __init__(self, templates):
        self.templates = templates
        self.aliases = {}
        for name, template in templates.items():
            for alias in [name, *template.get("aliases", [])]:
                self.aliases[alias.casefold()] = name

    def resolve(self, description):
        return self.aliases.get(description.casefold())

    def supports(self, capabilities, description):
        capability = self.resolve(description)
        return capability is not None and capability in capabilities

    def retrieve(self, descriptions, query):
        # Replaceable retrieval interface; exact supports() remains mandatory.
        return [name for name, caps in descriptions.items() if self.supports(caps, query)]
