# Contribuindo para o ticketz-sidekick2

Obrigado por considerar contribuir! 🎉

## Como Contribuir

1. **Fork** o projeto
2. Crie uma **branch** para sua feature (`git checkout -b feature/MinhaFeature`)
3. **Commit** suas mudanças (`git commit -m 'Add: descrição da feature'`)
4. **Push** para a branch (`git push origin feature/MinhaFeature`)
5. Abra um **Pull Request**

## Padrões de Commit

Use prefixos claros:
- `Add:` nova funcionalidade
- `Fix:` correção de bug
- `Docs:` documentação
- `Refactor:` refatoração sem mudança de comportamento
- `Test:` adicionar testes

## Reportar Bugs

Ao reportar bugs, inclua:
- Versão do sistema operacional
- Versão do Docker e Docker Compose
- Logs relevantes (output do sidekick2)
- Passos para reproduzir

## Sugestões de Features

Abra uma issue descrevendo:
- Problema que resolve
- Como funcionaria
- Exemplos de uso

## Testes

Antes de enviar PR:
1. Teste em ambiente não-produção
2. Execute `import --dry-run` para validar mudanças no import
3. Teste backup/restore em uma instalação limpa
4. Valide que o backup gerado é compatível com o restore padrão

## Dúvidas?

Abra uma issue ou discussion no GitHub!
