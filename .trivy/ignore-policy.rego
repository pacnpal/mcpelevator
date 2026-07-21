# Trivy ignore policy — consumed via the `ignore-policy` input of the Security
# Scan jobs in .github/workflows/docker-image.yml (release-time scan) and
# .github/workflows/security-scan.yml (scheduled re-scan), alongside the
# per-CVE suppressions in .trivyignore.yaml.
#
# Purpose: drop every finding Trivy attributes to the `linux-libc-dev` package.
#
# Why this is safe — and why it is package-scoped rather than a list of CVE IDs:
#
#   * linux-libc-dev ships ONLY userspace kernel headers (/usr/include/linux/*).
#     It contains no kernel code and nothing that executes. It is pulled into the
#     image transitively by the native build toolchain
#     (build-essential -> libc6-dev -> linux-libc-dev, Dockerfile), which exists
#     so npx/uvx MCP servers can compile native modules at runtime — the headers
#     are a compile-time input, never a running component.
#
#   * A container runs on the HOST's kernel, not on anything from this package.
#     The CVEs Trivy maps here are Linux *kernel* bugs (KVM, IPv6, NFSD, KEYS,
#     9p, skmsg, mac802154, ...) that live in the running kernel. None are
#     reachable through compile-time headers, and bumping the header package
#     changes no running code. Remediation for these is host-side (patch the
#     host kernel); no change to this image can deliver it.
#
#   * The kernel CNA discloses these continuously — the open alert set for this
#     one package grew from 4 to 10 in a single afternoon. Enumerating CVE IDs
#     in .trivyignore.yaml is therefore an unwinnable treadmill that leaves a
#     constant churn of open, non-actionable HIGH alerts. Scoping to the single
#     headers-only package suppresses the whole class without hiding anything
#     reachable. If a genuinely reachable, image-fixable linux-libc-dev finding
#     ever appears, it would need this policy revisited — an acceptable trade
#     given the package executes nothing.
package trivy

import rego.v1

default ignore := false

ignore if input.PkgName == "linux-libc-dev"
