# 🤝 Contributing Guide

Danke dass du zum Highrise Bot beitragen möchtest!

## 📋 Regeln

1. **Quellenangabe:** Der Originalentwickler ist [ImperatorKeksi](https://github.com/ImperatorKeksi). Wenn du den Code forkst oder weiterentwickelst, muss dieser Hinweis erhalten bleiben.

2. **Keine Passwörter/Secrets:** Committe NIEMALS `.env` Dateien, Tokens, API-Keys oder Passwörter.

3. **Code-Qualität:** 
   - Kommentiere komplexen Code
   - Schreibe deutschsprachige Kommentare und Commit-Messages
   - Teste deine Änderungen vor dem Push

## 🔀 Workflow

1. Fork das Repository
2. Erstelle einen Feature-Branch: `git checkout -b feature/meine-funktion`
3. Committe deine Änderungen: `git commit -m "Beschreibung"`
4. Pushe zum Branch: `git push origin feature/meine-funktion`
5. Erstelle einen Pull Request

## 🐛 Bug Reports

Erstelle ein Issue mit:
- Beschreibung des Fehlers
- Schritten zur Reproduktion
- Erwartetes vs. tatsächliches Verhalten
- Log-Auszüge (ohne Secrets!)

## 📝 Code Style

- Python: PEP 8
- Kommentare: Deutsch
- Variablen: `snake_case`
- Klassen: `PascalCase`
- Funktionen: `snake_case`

## 🔒 Sicherheit

- Alle Secrets kommen in `.env` (niemals in Code!)
- Die `.env.example` als Vorlage verwenden
- Bei Sicherheitslücken: Privat melden, nicht öffentlich
