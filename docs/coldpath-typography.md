# Coldpath typography

User requested a futuristic commercial type system on the existing midnight/cobalt/lime design. Space Grotesk variable (300–700) supplies geometric display headings and branding; Manrope variable (200–800) supplies readable UI and source text. Both are self-hosted under ui/fonts, preloaded on the three entry pages and licensed under the bundled SIL Open Font License notices. Font-display swap and system fallbacks keep text visible while files load. No third-party font requests, tracking, paid services or changes to underlying evidence.

Sources retrieved 2026-10-31 from the official Google Fonts repository:
- https://github.com/google/fonts/tree/main/ofl/spacegrotesk
- https://github.com/google/fonts/tree/main/ofl/manrope

- Manrope-Variable.ttf: SHA-256 3ae11c49db0455a3cc33e37d380f20fdb8c7f8b41dc07625c177e3d87a9d6ae6
- SpaceGrotesk-Variable.ttf: SHA-256 acad6de1fc93436f5c0f1f4137751ef04f1aea3063e7036535970ffcfbd79f72

Font files are explicitly binary in Git. Static synchronization and export include fonts and their license notices. Existing responsive runner browser journeys now await font loading and require the chosen families before screenshots. Exact-source public-release approval, GitHub-only browser/visual review and deployed-source verification remain required; no local browser or desktop control is used.
