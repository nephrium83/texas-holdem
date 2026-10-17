# Invite IPv4 compatibility

The V2 invite remains a 70-byte payload with four network-order bytes per
IPv4 address. Its codec does not import `socket` or resolve hostnames.
Decoding always renders a nonzero address as dotted decimal. A zero address
still means no address, and suppresses its port even when those port bytes
are nonzero.

Generation preserves the numeric forms previously accepted by
`socket.inet_aton`: one to four components with 32, 8+24, 8+8+16 or
8+8+8+8 bits respectively; decimal, leading-zero octal and `0x`/`0X`
hexadecimal components may be mixed. It retains ASCII whitespace suffix
handling, including ignoring text after the first ASCII whitespace. Leading
whitespace, signs, Unicode digits or whitespace, missing components,
out-of-range values and invalid base digits are refused. Embedded NULs
raise `ValueError`; non-string addresses raise `TypeError`; malformed
numeric addresses raise `OSError`, as before. UTF-8 conversion happens
before numeric parsing, preserving errors for unpaired surrogates even in
otherwise ignored suffixes.

Windows CPython uses an `inet_addr` fallback whose all-ones result also
signals failure. It accepts the exact string `255.255.255.255`, but rejects
equivalent forms such as `4294967295`, `0xffffffff`, `255.16777215` and even
`255.255.255.255 ` with a trailing space. The codec retains that exception
on `sys.platform == "win32"`; other platforms accept valid numeric
all-ones forms. This keeps BWING's accepted input set without imposing the
Windows failure sentinel on other platforms. The behavior was checked
against BWING's Windows Python 3.13.3, including component bounds,
octal/hexadecimal notation, ASCII and Unicode whitespace, long zero
prefixes, suffixes, NULs and input types. The invite tests pin this policy
and compare a generated numeric corpus against the local socket codec.

That Windows fallback also accepts exactly one ASCII space as zero; the
codec preserves this case on Windows. Empty strings, two spaces, a lone
tab or newline, and whitespace before an address remain invalid. This is
an exact input exception, not general leading-whitespace stripping. The
all-ones distinction also appears in CPython's
[fallback implementation](https://github.com/python/cpython/blob/v3.13.3/Modules/socketmodule.c).
