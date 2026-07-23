You receive the opening text of a book. Identify the book's title, its author, and the language the text is written in, keeping title and author in the book's original language. Respond with ONLY a JSON object, no code fences, in this exact shape:

{"title": "...", "author": "...", "language_name": "...", "language": "..."}

`language_name` must be the full English name of the language the text is written in (e.g. "English", "Italian", "Bulgarian"). `language` must be the BCP-47 language code for that same language (e.g. "en", "it", "bg") — state the full `language_name` first, then derive `language` from it. If the title or author cannot be determined from the text, use null for that field. If the language cannot be determined, use null for both language fields.
