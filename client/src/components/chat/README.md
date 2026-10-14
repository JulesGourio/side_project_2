# Chat

Interface du chatbot : une question, une réponse rédigée à partir de la base
documentaire, avec des sources cliquables. Le dossier contient les vues, le
flux de réponse et la gestion des conversations. Le serveur (route WebSocket,
moteur de recherche, persistance) est décrit dans `server/routers/chat/` et
`server/services/chat/`.

## Fichiers

| Fichier | Rôle |
|---|---|
| `ChatView.tsx` | Mise en page : barre latérale, messages, saisie, partage ; ouvre une conversation passée en `?session=` |
| `useChatConversation.ts` | État d'une conversation : messages, tour en cours, chargement, arrêt, partage |
| `useChatSessions.ts` | Liste des conversations de l'utilisateur (`/api/chat/sessions`), suppression |
| `chatStream.ts` | Connexion WebSocket d'un tour, réception des trames, texte d'erreur `TIRED_MESSAGE` |
| `message.ts` | Types `Message`, `Source`, `Feedback` et libellé de version |
| `division.tsx` | Type `Division` (ALL / AS / IS), sélecteur et retrait du préfixe `[Division: …]` |
| `ChatSidebar.tsx` | Liste des conversations avec leur date relative, nouvelle conversation, choix de la division |
| `ChatInput.tsx` | Zone de saisie, envoi, bouton d'arrêt |
| `ChatMessage.tsx` | Bulle d'un message : texte rendu, sources, retour de l'utilisateur, export |
| `ChatSources.tsx` | Pastilles des documents cités, variantes linguistiques regroupées |
| `ChatFeedback.tsx` | Vote pouce haut / bas avec commentaire facultatif ; version en lecture seule |
| `DownloadMenu.tsx` | Export de la réponse ou de la conversation en Markdown ou PDF |
| `chatExport.ts` | Construction du Markdown et des sections PDF à partir des messages |
| `WelcomeState.tsx` | Écran d'accueil d'une conversation vide, avec des questions suggérées |

Pages qui utilisent ce dossier :
[`ChatPage.tsx`](../../pages/ChatPage.tsx) (route `/chat`) et
[`SharedChatPage.tsx`](../../pages/SharedChatPage.tsx) (route `/chat/shared/:token`,
lecture seule).

## Déroulement d'un tour

1. `useChatConversation.send` ajoute la question et une bulle de réponse en cours
   de streaming, puis envoie tout l'historique de la conversation à `streamChat`.
2. `streamChat` ouvre `/api/chat/ws` (`wss:` ou `ws:` selon le protocole de la
   page) et envoie une seule trame JSON : `messages`, `session_id`, `division`.
3. Le serveur répond par des trames JSON, traitées dans `chatStream.ts` :

   | Trame | Effet côté client |
   |---|---|
   | `delta` (`delta`) | Le texte s'ajoute à la bulle |
   | `status` (`status`) | Mise à jour du libellé d'attente ; `still_searching` affiche « Still searching » |
   | `ping` | Keepalive du serveur pendant une longue attente, ignoré |
   | `done` (`session_id`, `message_id`, `content`, `sources`) | Le texte final et les sources remplacent le texte streamé ; `onTurnSaved` recharge la liste des conversations |
   | `error` (`error`) | La bulle passe en erreur avec le texte reçu, ou `TIRED_MESSAGE` à défaut |

4. Un tour se termine toujours par exactement un résultat : `done`, `error`, ou
   l'arrêt demandé par l'utilisateur. Une socket qui se ferme sans l'un de ces
   trois résultats affiche `TIRED_MESSAGE` au lieu de rester sur « Thinking ».
5. Une socket qui échoue **avant** l'envoi de la question est rouverte jusqu'à
   deux fois (attente de 1 s, puis 2 s). Après l'envoi, il n'y a pas de nouvelle
   tentative.

Le texte streamé est remplacé à `done` par le contenu enregistré par le serveur,
qui contient les marqueurs `⟦n⟧` de citation.

## Règles

- **Un seul message d'erreur.** L'utilisateur voit `TIRED_MESSAGE`, identique au
  texte serveur ; la cause technique est journalisée côté serveur et n'est jamais
  affichée.
- **Division.** Le choix ALL / AS / IS de la barre latérale part avec chaque tour.
  Le filtre est appliqué par le serveur sur la colonne `division` de l'index : le
  périmètre ne dépend pas de la question. `stripDivision` retire un éventuel
  préfixe `[Division: …]` avant l'affichage d'une question.
- **Citations.** Dans le texte, chaque marqueur `⟦n⟧` devient un exposant cliquable
  vers la source `n` (`MarkdownRenderer`, table `citations` construite par
  `ChatMessage` à partir des sources numérotées). Les exports remplacent les
  marqueurs par `[n]`.
- **Sources.** Une pastille par document. Une source citée dans le texte porte son
  numéro ; une source seulement mentionnée en prose n'en porte pas. Une pastille
  avec lien ouvre le document dans un nouvel onglet ; sans lien, c'est un badge
  simple dont l'info-bulle donne la référence complète.
- **Variantes linguistiques.** Les REF qui ne diffèrent que par un suffixe de langue
  (`_FR`, `_GB`, `_EN`, …) sont regroupés dans une seule pastille, avec un drapeau
  par langue (`canonicalDocKey`). Le libellé est le code commun, sans suffixe. L'ordre
  d'affichage ne désigne pas une langue par défaut.
- **Détail d'une source.** L'info-bulle reprend le titre, la révision et la date du
  document, fournis par le serveur (`Source.doc_title`, `revision`, `doc_date`).
- **Arrêt.** « Arrêter » ferme la socket. La bulle garde le texte déjà reçu, sans
  message d'erreur ; le serveur enregistre le tour comme interrompu.
- **Conversation neuve pendant un tour.** Elle interrompt le tour en cours.
- **Partage.** Le bouton de partage n'agit que sur une conversation déjà enregistrée
  côté serveur : il crée le lien `/chat/shared/<jeton>`, copié dans le presse-papiers.
- **Vue partagée.** Lecture seule. Le lecteur voit le vote du propriétaire, mais ne
  peut pas voter. « Dupliquer » crée une copie dans les conversations du lecteur et
  l'ouvre (`/chat?session=…`) ; la conversation partagée n'est pas modifiée.
- **Retour utilisateur.** Un vote par message, envoyé une seule fois, avec un
  commentaire facultatif (`/api/chat/feedback`). Après l'envoi, le bloc affiche une
  confirmation.
- **Export.** Réponse seule ou conversation entière, en Markdown ou PDF. Le PDF est
  rendu par le serveur (`/api/export/pdf`, `lib/exportPdf.ts`) ; le Markdown est
  construit côté client.
- **Liste des conversations.** Chaque conversation affiche sa date relative
  (« Today », « Yesterday », « N days ago », puis une date courte), calculée en jours
  calendaires dans le fuseau de l'utilisateur. La liste n'apparaît que si le serveur
  signale que l'historique est disponible.
- **Date des documents.** `ChatView` affiche la date de mise à jour des documents
  (`/api/config/knowledge-base-date`) à côté du titre.

## Accès

- `ChatPage` : si l'utilisateur n'a pas le droit de chat (`can_chat`), il voit le
  panneau d'accès refusé ; si le chat est désactivé dans la configuration
  (`chat.enabled`), il voit un écran d'attente.
- Côté WebSocket, le serveur répond par une trame `error` (« Chat access not
  granted » ou « Chat is disabled ») et ferme la socket. Les routes HTTP renvoient
  403.

## Routes utilisées

| Appel | Usage |
|---|---|
| `WS /api/chat/ws` | Un tour de chat (`chatStream.ts`) |
| `GET /api/chat/sessions` | Liste des conversations (`useChatSessions`) |
| `GET /api/chat/sessions/{id}` | Chargement d'une conversation |
| `DELETE /api/chat/sessions/{id}` | Suppression d'une conversation |
| `POST /api/chat/sessions/{id}/share` | Création ou récupération du jeton de partage |
| `GET /api/chat/shared/{token}` | Vue partagée (`SharedChatPage`) |
| `POST /api/chat/shared/{token}/duplicate` | Copie d'une conversation partagée |
| `POST /api/chat/feedback` | Vote sur un message |
| `GET /api/config/knowledge-base-date` | Date des documents |
| `POST /api/export/pdf` | Export PDF |

Les tables Lakebase correspondantes sont décrites dans
[`docs/lakebase_schema.md`](../../../../docs/lakebase_schema.md).
