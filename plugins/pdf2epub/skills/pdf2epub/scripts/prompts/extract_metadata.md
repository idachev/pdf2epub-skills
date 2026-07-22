You receive the opening text of a book. Identify the book's title, its author, and the language the text is written in, keeping title and author in the book's original language. Respond with ONLY a JSON object, no code fences, in this exact shape:

{"title": "...", "author": "...", "language": "..."}

`language` must be a BCP-47 language code (e.g. "en", "bg", "fr", "de", "es") reflecting the actual language of the text. If the title or author cannot be determined from the text, use null for that field. If the language cannot be determined, use null.
