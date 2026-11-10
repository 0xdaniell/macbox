# Disclaimer and third-party notices

## Warranty and liability

macbox's own code and documentation are provided **as is** and **as available**. To the extent
permitted by applicable law, the licensor disclaims all warranties and conditions, express or
implied, including merchantability, fitness for a particular purpose, title, non-infringement,
availability, security, accuracy and freedom from errors. There is no promise of support, maintenance,
updates or uninterrupted operation.

The [PolyForm Noncommercial License](LICENSE#no-liability) disclaims the licensor's liability to
you to the extent the law allows. This includes, to that extent, claims arising from use or inability
to use macbox, whether in contract, tort or otherwise, and losses such as lost data, profits or
business, service interruption, and direct, indirect, incidental or consequential damages. Nothing
in this notice excludes or limits rights or liability that applicable law does not allow to be
excluded or limited. This notice does not change the license or the separate terms of third-party
software.

## Operational risks

Virtual machines, GPU passthrough, host configuration changes, scripts, updates and third-party
downloads can cause data loss, corruption, downtime, security exposure or unexpected costs. For
example, `MACBOX_DISK_CACHE=unsafe` can cause data loss if the host crashes. Review commands and
configuration before running them, restrict access to VM and remote-control interfaces, and keep
independent backups. You are responsible for deciding whether the software is suitable for your
environment and for protecting your data and credentials.

## Apple and other third parties

macbox does not distribute macOS, Xcode or iOS Simulator runtimes. They are obtained from Apple and
are subject to Apple's terms. macbox does not grant you rights to Apple software, services, trademarks
or accounts, or permission to bypass any restriction. Apple's macOS license restricts installation
and use to Apple-branded hardware and imposes additional conditions on virtualization. Running macOS
on other hardware can breach that license. Consult the license for your version and any other
applicable agreements before use: [Apple software licenses](https://www.apple.com/legal/sla/) and
[Apple developer agreements](https://developer.apple.com/support/terms/).

You are responsible for obtaining the permissions needed for your use and complying with applicable
law and third-party terms. Neither this notice nor use of macbox waives any third party's rights.
macbox is not affiliated with, sponsored by or endorsed by Apple Inc. macOS, iOS, Xcode and Metal are
trademarks of Apple Inc. Other components retain their own licenses; see [credits](docs/credits.md)
and [patch licenses](src/patches/README.md).
