def button_label():
    return "Submit"


def normalize(text):
    return text.strip()


def search(items, query):
    return [item for item in items if query in item]
