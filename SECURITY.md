# Security

Do not include credentials, authorization codes, real bookmark inventories, or
private post content in public issues. Use synthetic reproductions.

Report vulnerabilities through GitHub's **Security → Report a vulnerability** on
this repository. Never post a vulnerability containing credentials as a public issue.
If a token is exposed, revoke the app's access in X and authorize again; deleting a
file or commit does not revoke a token.

This project is pre-1.0. Security fixes target the latest version on `main`.
Downloaded content and local export state are private application data. Keep them
outside source control and protect the destination directory appropriately.
