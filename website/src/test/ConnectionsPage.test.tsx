import { describe, expect, it } from 'vitest'
import type { ChatMessage, McpServer } from '../types'
import {
  connectionStateFor,
  disconnectFeedback,
  effectiveOAuth,
  isValidLoopbackReturnAddress,
  latestOAuthByServer,
  mintOutcome,
  uninstallOnCancel,
  withMintedUrl,
  type OAuthState,
  type PendingConnect,
} from '../pages/connections/ConnectionsPage'

const server = (status: string): McpServer => ({
  name: 'notion',
  command: '',
  url: 'https://mcp.notion.com/mcp',
  status,
  source: 'mcp.json',
  enabled: true,
})

describe('Connections card states', () => {
  it('covers not connected, waiting, connected, and needs-attention states', () => {
    expect(connectionStateFor(undefined, undefined)).toBe('not-connected')
    expect(connectionStateFor(server('unknown'), undefined)).toBe('waiting-for-approval')
    expect(connectionStateFor(server('ok'), undefined)).toBe('connected')
    expect(connectionStateFor(server('error'), undefined)).toBe('needs-attention')
  })

  it('keeps a newly added authorization error waiting until OAuth resolves', () => {
    expect(connectionStateFor(server('error'), undefined, true)).toBe('waiting-for-approval')
    expect(connectionStateFor(server('error'), {
      completed: false,
      failed: true,
      oauthUrl: '',
      error: 'denied',
      timestamp: 1,
    }, true)).toBe('needs-attention')
  })
})

/**
 * The chat message IS the card's approval-URL feed.
 *
 * The backend tags a request `card_owned` when this gallery owns the consent
 * flow; the tag tells the CHAT renderer not to duplicate the prompt, and the
 * message is still delivered precisely so the card can read the URL out of it.
 * If this ever filtered on the tag, connecting a provider would hand the user a
 * card with no Authorize link and no other surface offering one.
 */
describe('card-owned OAuth messages feed the card', () => {
  const oauthMsg = (meta: Record<string, unknown>): ChatMessage => ({
    role: 'mcp_oauth',
    content: '🔐 notion requires authentication.',
    cls: 'msg msg-info',
    ts: '2026-08-10T00:00:00Z',
    meta,
  })

  it('reads the approval URL off a card_owned message', () => {
    const feed = latestOAuthByServer(
      [oauthMsg({
        server_name: 'notion',
        oauth_url: 'https://mcp.notion.com/authorize?state=x',
        card_owned: true,
      })],
      {},
    )
    expect(feed.notion.oauthUrl).toBe('https://mcp.notion.com/authorize?state=x')
    expect(connectionStateFor(server('unknown'), feed.notion)).toBe('waiting-for-approval')
  })

  it('reads an unannotated message identically', () => {
    const feed = latestOAuthByServer(
      [oauthMsg({ server_name: 'notion', oauth_url: 'https://mcp.notion.com/authorize?state=x' })],
      {},
    )
    expect(feed.notion.oauthUrl).toBe('https://mcp.notion.com/authorize?state=x')
  })
})

/**
 * A minted URL is the feed for a card-initiated connect, and it is folded in
 * AFTER the banner staleness fence — a mint belongs to the click being served,
 * so it must never be discarded as a leftover from a prior attempt.
 */
describe('mint outcome table', () => {
  const newFlow: PendingConnect = { kind: 'new', sinceTs: 0 }
  const preExisting: PendingConnect = { kind: 'reconnect', sinceTs: 0 }
  const held = { clearWait: false, probe: false, error: false, uninstall: false }

  it.each([undefined, 'idle', 'minting', 'waiting'] as const)(
    'holds the wait for %s',
    state => {
      const row = state === undefined ? undefined : { slug: 'notion', state }
      expect(mintOutcome(row, newFlow)).toEqual(held)
      expect(mintOutcome(row, preExisting)).toEqual(held)
    },
  )

  it('probes on granted so the pre-consent error is replaced', () => {
    for (const pending of [newFlow, preExisting]) {
      expect(mintOutcome({ slug: 'notion', state: 'granted' }, pending)).toEqual({
        clearWait: true, probe: true, error: false, uninstall: false,
      })
    }
  })

  it('surfaces an error on failed and keeps the entry for a retry', () => {
    for (const pending of [newFlow, preExisting]) {
      expect(mintOutcome({ slug: 'notion', state: 'failed' }, pending)).toEqual({
        clearWait: true, probe: false, error: true, uninstall: false,
      })
    }
  })

  it('uninstalls on expired when THIS flow installed the entry', () => {
    // Otherwise the card lands on "Needs attention" instead of idle Connect.
    expect(mintOutcome({ slug: 'notion', state: 'expired', token: 7 }, { ...newFlow, token: 7 }))
      .toEqual({ clearWait: true, probe: false, error: false, uninstall: true })
  })

  it('does NOT uninstall when the expired row belongs to another tab', () => {
    // Two tabs, one slug-keyed row: Tab B superseded Tab A, so B's expiry must not
    // remove the entry A installed. The wait still clears -- this attempt is over.
    expect(mintOutcome({ slug: 'notion', state: 'expired', token: 9 }, { ...newFlow, token: 7 }))
      .toEqual({ clearWait: true, probe: false, error: false, uninstall: false })
  })

  it('does NOT uninstall when either side has no token', () => {
    expect(mintOutcome({ slug: 'notion', state: 'expired' }, { ...newFlow, token: 7 }).uninstall)
      .toBe(false)
    expect(mintOutcome({ slug: 'notion', state: 'expired', token: 7 }, newFlow).uninstall)
      .toBe(false)
  })

  it('never uninstalls a pre-existing entry, or one of unknown origin', () => {
    const row = { slug: 'notion', state: 'expired' as const, token: 7 }
    expect(mintOutcome(row, { ...preExisting, token: 7 }).uninstall).toBe(false)
    expect(mintOutcome(row, undefined).uninstall).toBe(false)
  })

})

describe('minted approval URLs', () => {
  const minted = 'https://mcp.notion.com/authorize?state=minted'

  it('supplies the URL when no banner has arrived', () => {
    const merged = withMintedUrl(undefined, { slug: 'notion', state: 'waiting', oauth_url: minted })
    expect(merged?.oauthUrl).toBe(minted)
    expect(connectionStateFor(server('unknown'), merged)).toBe('waiting-for-approval')
  })

  it('survives the staleness fence that discards a stale banner', () => {
    const stale: OAuthState = {
      completed: false, failed: false, oauthUrl: 'https://old.example/authorize', error: '', timestamp: 5,
    }
    const fenced = effectiveOAuth(stale, { kind: 'new', sinceTs: 10 })
    expect(fenced).toBeUndefined()
    expect(withMintedUrl(fenced, { slug: 'notion', state: 'waiting', oauth_url: minted })?.oauthUrl)
      .toBe(minted)
  })

  it('leaves an existing banner URL in place', () => {
    const banner: OAuthState = {
      completed: false, failed: false, oauthUrl: 'https://banner.example/authorize', error: '', timestamp: 1,
    }
    expect(withMintedUrl(banner, { slug: 'notion', state: 'waiting', oauth_url: minted })?.oauthUrl)
      .toBe('https://banner.example/authorize')
  })

  it('preserves a terminal banner verdict while filling in the URL', () => {
    const failed: OAuthState = {
      completed: false, failed: true, oauthUrl: '', error: 'denied', timestamp: 1,
    }
    const merged = withMintedUrl(failed, { slug: 'notion', state: 'waiting', oauth_url: minted })
    expect(merged?.failed).toBe(true)
    expect(merged?.error).toBe('denied')
  })

  it.each(['idle', 'minting', 'granted', 'failed', 'expired'] as const)(
    'offers no URL in the %s state',
    state => {
      // Only `waiting` holds a redeemable URL; `expired` in particular carries
      // one the backend has already judged unredeemable.
      expect(withMintedUrl(undefined, { slug: 'notion', state, oauth_url: minted })).toBeUndefined()
    },
  )

  it('is a no-op when no mint exists', () => {
    expect(withMintedUrl(undefined, undefined)).toBeUndefined()
  })

  it('marks a minted URL so the card can drop the browser-tab copy', () => {
    // The mint opens no tab, so "finish approving in your browser" would send the
    // user looking for a window that never existed.
    const merged = withMintedUrl(undefined, { slug: 'notion', state: 'waiting', oauth_url: minted })
    expect(merged?.minted).toBe(true)
  })

  it('leaves a banner-sourced URL unmarked', () => {
    const banner: OAuthState = {
      completed: false, failed: false, oauthUrl: 'https://banner.example/authorize', error: '', timestamp: 1,
    }
    expect(withMintedUrl(banner, { slug: 'notion', state: 'waiting', oauth_url: minted })?.minted)
      .toBeUndefined()
  })
})

/**
 * A mint that ends without a URL must end the wait too. Consuming only `waiting`
 * left the card spinning forever on a failed or TTL-expired mint, with the poll
 * still running and copy telling the user to do something that cannot help.
 */

describe('stale OAuth banner fencing', () => {
  const banner = (timestamp: number, completed = true): OAuthState => ({
    completed,
    failed: !completed,
    oauthUrl: '',
    error: completed ? '' : 'denied',
    timestamp,
  })

  it('ignores banners at or below the click-time snapshot', () => {
    // The banner observed at click time (ts 100) must not resurface…
    const same = effectiveOAuth(banner(100), { kind: 'reconnect', sinceTs: 100 })
    expect(same).toBeUndefined()
    expect(connectionStateFor(server('unknown'), same, true)).toBe('waiting-for-approval')
    // …nor anything even older.
    expect(effectiveOAuth(banner(50), { kind: 'reconnect', sinceTs: 100 })).toBeUndefined()
  })

  it('honors banners raised after the snapshot', () => {
    const fresh = banner(300)
    expect(effectiveOAuth(fresh, { kind: 'reconnect', sinceTs: 100 })).toBe(fresh)
    expect(connectionStateFor(server('unknown'), fresh, true)).toBe('connected')
  })

  it('accepts the first banner when none existed at click time', () => {
    const first = banner(1)
    expect(effectiveOAuth(first, { kind: 'new', sinceTs: 0 })).toBe(first)
  })

  it('leaves banners untouched when no attempt is pending', () => {
    const oauth = banner(100)
    expect(effectiveOAuth(oauth, undefined)).toBe(oauth)
  })
})

describe('cancel semantics', () => {
  it('uninstalls only a cancelled new connect', () => {
    expect(uninstallOnCancel({ kind: 'new', sinceTs: 0 })).toBe(true)
    expect(uninstallOnCancel({ kind: 'reconnect', sinceTs: 0 })).toBe(false)
    expect(uninstallOnCancel(undefined)).toBe(false)
  })
})

describe('disconnect feedback', () => {
  it('keeps the provider revoke destination after removing the local entry', () => {
    expect(disconnectFeedback({
      name: 'Notion',
      revoke_page_url: 'https://www.notion.so/my-integrations',
    }, 'Disconnected locally.')).toEqual({
      kind: 'success',
      text: 'Disconnected locally.',
      revoke: {
        href: 'https://www.notion.so/my-integrations',
        provider: 'Notion',
      },
    })
  })
})

describe('loopback OAuth return-address validation', () => {
  it('accepts only an IP-literal loopback callback with a port and code', () => {
    expect(isValidLoopbackReturnAddress('http://127.0.0.1:43123/?code=one-time&state=s')).toBe(true)
    expect(isValidLoopbackReturnAddress('http://[::1]:43123/callback?code=one-time')).toBe(true)
  })

  it.each([
    'https://127.0.0.1:43123/?code=x',
    'http://localhost:43123/?code=x',
    'http://10.0.0.5:43123/?code=x',
    'http://127.0.0.1/?code=x',
    'http://127.0.0.1:43123/',
    'http://user@127.0.0.1:43123/?code=x',
    'http://127.0.0.1:43123/?code=x&code=y',
    'http://127.0.0.1:43123/?code=x#fragment',
  ])('rejects unsafe or incomplete return address %s', value => {
    expect(isValidLoopbackReturnAddress(value)).toBe(false)
  })
})
