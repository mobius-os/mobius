# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Möbius is for a mixed audience, including people who are not technical and may
be cautious around infrastructure, authentication, and agent setup. They want a
personal system they can use and shape without first learning how it is hosted.

## Product Purpose

Möbius gives each person a private, persistent place for conversations, apps,
files, memory, and agentic work. First-run success means reaching the working
Möbius shell quickly; connecting an agent and installing more apps are optional,
contextual next steps.

## Positioning

Möbius is a personal computing environment whose agent can build and change the
apps and platform around it. The user retains control of the deployment and can
choose how it is hosted and authenticated.

## Operating Context

Users may enter through a managed Railway deployment or install Möbius
themselves. Managed deployments can reuse the mobius.you identity; self-hosted
installs use a username and password created on first boot. Agent providers are
connected later from Settings. Apps are discovered through the drawer and App
Store.

## Capabilities and Constraints

- A usable shell does not require an AI provider to be connected.
- Chat and agentic actions do require a configured provider.
- OpenAI Codex on a headless server uses device-code authorization.
- Provider credentials and user data stay with the Möbius instance.
- First-run guidance must be dismissible and must not block exploration.

## Brand Commitments

Use the Möbius name and existing mark. Explain technical boundaries in plain,
calm language. Treat ownership, freedom, privacy, and user choice as product
truth rather than decorative claims.

## Evidence on Hand

The existing shell, Settings provider controls, drawer, App Store integration,
and first-sign-in walkthrough are implemented in `frontend/src`. Do not invent
customer claims, benchmarks, or privacy guarantees beyond the documented
deployment boundary.

## Product Principles

- Open the product before teaching the product.
- Make advanced capability available, never compulsory.
- Put guidance beside the action it explains.
- Be exact about where data and credentials live.
- Preserve user ownership and reversibility.

## Accessibility & Inclusion

The experience must work with keyboard navigation, reduced motion, narrow
mobile screens, and clear language for people unfamiliar with hosting or AI
provider terminology.
