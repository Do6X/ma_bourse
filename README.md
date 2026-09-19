# Backend — API Conseil d'Investissement

Implémente le workflow du cahier des charges : `GET /api/advice?symbol=AAPL`
récupère les données (Yahoo Finance, bascule automatique vers Financial
Modeling Prep en cas d'échec), calcule les indicateurs techniques et
fondamentaux, analyse le sentiment, et retourne le conseil, le niveau de
risque et la justification détaillée.

## Démarrage rapide

```bash
pip install -r requirements.txt

# 1. Générer une clé maître (à conserver en lieu sûr, JAMAIS dans le code)
export APP_MASTER_KEY=$(python secrets_util.py generate-master-key)

# 2. Chiffrer votre clé FMP (générez-en une NOUVELLE : l'ancienne, transmise
#    en clair dans la conversation d'origine, doit être considérée comme
#    compromise et révoquée depuis votre tableau de bord FMP)
python secrets_util.py set FMP_API_KEY "votre_nouvelle_cle_fmp"

# 3. (Optionnel) sentiment via NewsAPI plutôt que le repli neutre par défaut
python secrets_util.py set NEWSAPI_KEY "votre_cle_newsapi"

# 4. Lancer le serveur
uvicorn app:app --reload --port 8000
```

Test rapide : `curl "http://localhost:8000/api/advice?symbol=AAPL"`

## Endpoints

| Route | Description |
|---|---|
| `GET /api/advice?symbol=AAPL` | Analyse complète d'une valeur (séance, technique, fondamental, sentiment, conseil, risque, dividendes, stratégie) — endpoint de référence, structure imbriquée. |
| `GET /api/actions/{ticker}` | Même analyse, alias en JSON plat conforme au contrat `GET /api/actions/{ticker}` décrit dans la synthèse de spécifications (`nom`, `cours_ouverture`, `rsi`, `conseil`, `niveau_risque`, etc.). Utilisez `/api/advice` si vous avez besoin du détail complet. |
| `GET /api/advice/batch?symbols=AAPL,MSFT,TSLA` | Idem, groupé — un seul appel Yahoo pour tous les tickers (`yfinance.download`) au lieu d'un aller-retour par valeur. |
| `GET /api/alerts?symbol=TSLA` | Évalue les règles d'alerte par défaut (RSI > 70, RSI < 30, risque ≥ 8, conseil Acheter/Vendre) et retourne celles déclenchées. |
| `GET /api/health` | Vérification de service + backend de cache actif (`redis` ou `file`). |

## Valeurs Euronext Paris (CAC 40, etc.)

Ajoutez simplement le suffixe `.PA` au ticker — ex. `symbol=MC.PA` (LVMH),
`TTE.PA` (TotalEnergies), `AI.PA` (Air Liquide), `BNP.PA` (BNP Paribas),
`OR.PA` (L'Oréal), `SAN.PA` (Sanofi). Le format est celui utilisé par Yahoo
Finance et fonctionne directement dans le widget et sur `/api/advice` /
`/api/actions/{ticker}`.

**Ce qui fonctionne** pour ces valeurs (source : Yahoo Finance, pas
concerné par la restriction FMP ci-dessous) : cours de séance
(ouverture/clôture/plus haut/plus bas), historique, RSI/MACD/moyennes
mobiles/volume, tendances jour et 7 séances, et l'**éligibilité PEA**
(nouveau champ `eligibilite_pea`, voir plus bas).

**Ce qui ne fonctionne pas** avec le plan FMP actuel : tous les
endpoints FMP (`profile`, `ratios-ttm`, `key-metrics-ttm`,
`price-target-consensus`, `grades-consensus`, `dividends`,
`historical-price-eod`) renvoient une erreur 402 pour les tickers hors
bourses américaines — vérifié à nouveau le 06/09/2026 sur `MC.PA`. En
clair : PER, P/B, ROE, dette/équité, croissance du CA, dividendes, objectif
de cours consensus et recommandations d'analystes restent vides
(`null`) pour vos valeurs Euronext Paris tant que ce plan n'est pas mis à
niveau. Le score fondamental et le score sentiment se replient alors sur
une valeur neutre (5/10) plutôt que de planter ou d'inventer un chiffre —
seul le score technique (RSI, MACD, tendance, volume — 50 % de la note)
reste pleinement calculé. Pour lever cette limite : passer à un plan FMP
supérieur couvrant les bourses non américaines, ou brancher une source
alternative pour les fondamentaux/dividendes européens (non fait ici).

**Cas particulier EDF** : la valeur a été retirée d'Euronext Paris lors de
la renationalisation en juin 2023 — `EDF.PA` ne renverra plus aucune
donnée, ce n'est pas un bug de l'application.

## Éligibilité PEA (nouveau)

Chaque analyse inclut désormais un champ `eligibilite_pea` :
```json
"eligibilite_pea": {"estime": true, "pays_siege": "France", "avertissement": null}
```
`estime` vaut `true`/`false`/`null` (indéterminé si le pays du siège social
n'a pas pu être identifié). **C'est une estimation automatique, pas une
vérification officielle** : il n'existe pas de liste publique d'éligibilité
PEA consultable par API (ni AMF, ni Euronext, ni Bercy). La règle appliquée
(code monétaire et financier, art. L221-31) : siège social dans un État de
l'Union européenne, ou en Islande/Norvège/Liechtenstein, et assujettissement
à l'impôt sur les sociétés. Le pays est lu sur le profil Yahoo (`yfinance`)
quand FMP est indisponible (donc systématiquement pour vos valeurs
Euronext Paris, cf. ci-dessus).

Cas **non couverts** par cette estimation, à vérifier vous-même avant tout
achat en PEA :
- Foncières cotées à statut SIIC (ex. Unibail-Rodamco-Westfield, Gecina,
  Klépierre, Icade, Covivio) — généralement exclues du PEA même si
  françaises ; l'app affiche un avertissement quand le secteur détecté est
  "Real Estate", mais ne connaît pas le statut fiscal SIIC lui-même.
- Structures de holding complexes ou double cotation.
- Le Royaume-Uni n'est plus éligible depuis le Brexit (janvier 2021) — un
  avertissement spécifique s'affiche le cas échéant.

Ceci n'est pas un conseil fiscal ou juridique.

## Ce qui est fait vs ce qui reste à faire

**Fait** : logique de scoring (technique 50% / fondamental 30% / sentiment
20%), niveau de risque pondéré (30/20/20/15/15) avec volatilité annualisée
réelle, bascule Yahoo→FMP tracée dans les logs et signalée au frontend
(`source_bascule_api: true`), cache à deux niveaux de fraîcheur (5 min temps
réel / 1h historique, Redis si `REDIS_URL` est définie sinon fichiers
locaux), requêtes groupées multi-tickers, chiffrement des clés API au repos,
logs structurés (console + fichier tournant dans `logs/`), messages d'erreur
utilisateur explicites ("vérifiez votre connexion internet ou réessayez plus
tard") sur toute panne amont.

**Volontairement pas fait** (cahier des charges section D, réservé à un
usage prototype pour l'instant — voir échange précédent) : Firebase
Authentication, hachage de mot de passe (bcrypt/Argon2), consentement RGPD.
Ces éléments n'ont de sens qu'avec de vrais comptes multi-utilisateurs et un
hébergement dédié ; les ajouter maintenant produirait du code non testable
et non déployé. Si vous voulez ouvrir l'app à plusieurs utilisateurs,
prévenez-moi : c'est un chantier à part entière (gestion de session, base de
données des watchlists/alertes par utilisateur, politique de confidentialité).

**Écarts connus avec la synthèse de spécifications du 06/09/2026** (décision
prise avec l'utilisateur : rester sur ce stack léger — web + FastAPI —
plutôt que de reconstruire PostgreSQL/React Native/React.js) :
- Stockage : fichiers/cache local, pas PostgreSQL — pas de préférences ni
  de portefeuille persistant à chiffrer (aucune donnée utilisateur n'est
  encore stockée, donc rien à chiffrer pour l'instant).
- Frontend : page web unique (adaptative portrait/paysage, cf.
  `frontend/widget.html` et le tableau de bord de démonstration), pas
  d'application React Native ni de client React.js séparé.
- Graphiques : SVG fait main (léger, sans dépendance), pas Chart.js/D3.js —
  à remplacer facilement si un jour un vrai graphique interactif (zoom,
  tooltips) devient nécessaire.
- Actualités : NewsAPI + recherche web, pas Qwant — Qwant n'expose pas
  d'API d'actualités publique documentée à ce jour ; à réévaluer si une
  source européenne équivalente est identifiée.
- Performance temps réel : le cache `TTL_REALTIME` est fixé à 5 minutes,
  au-dessus du seuil de 15 secondes demandé — le descendre à 15s
  multiplierait par ~20 le volume d'appels FMP/Yahoo et ferait sauter le
  quota gratuit actuel (déjà en erreur 402 sur certains tickers). À
  ajuster seulement si un plan API payant est prévu.
- Accessibilité (WCAG contraste daltoniens, ≥16px) et tests
  unitaires/intégration formels : pas encore audités/écrits.

**HTTPS** : ce processus ne le fait pas lui-même — déployez-le derrière un
reverse proxy TLS (Caddy, Nginx, ou une plateforme qui le fait pour vous :
Render, Fly.io, Railway, etc.).

**Marché européen** : voir la section « Valeurs Euronext Paris » plus haut
pour le détail de ce qui fonctionne (technique, éligibilité PEA) et de ce
qui ne fonctionne pas encore (fondamentaux/dividendes/consensus, 402 sur le
plan FMP actuel).

## Sécurité — à vérifier avant toute mise en production

1. Ne jamais committer `.secrets.enc` ni `APP_MASTER_KEY` (voir `.gitignore`).
2. Restreindre `allow_origins` dans `app.py` au(x) domaine(s) réel(s) du
   frontend (actuellement `*` pour faciliter les tests locaux).
3. Ajouter une limite de débit (`slowapi` ou équivalent) si l'API devient
   publique, pour éviter l'épuisement de votre quota FMP/NewsAPI.
4. Révoquer et régénérer toute clé API qui a pu transiter en clair.
