/**
 * InstanceFormFields — the remote-crew field set, shared by the "add a crew"
 * form and the per-crew "edit" form.
 *
 * Both forms write the same record through the same validation, so the fields,
 * their hints, and the transport-conditional layout live here once. `idPrefix`
 * keeps DOM ids unique when an edit form is open on a row while the add form is
 * mounted further down the same page.
 */
import { useCallback, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Pencil } from 'lucide-react'
import { api, ApiError, type AddInstanceBody, type InstanceView } from '../../api/client'
import SimpleSelect from '../../components/SimpleSelect'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useAppDispatch } from '../../store'
import { setWarm } from '../../store/instancesSlice'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'

/** Form-shaped mirror of an instance record: every field is a string the user typed. */
export interface InstanceFormValues {
  name: string
  method: 'ssh' | 'ssm'
  sshHost: string
  ssmTarget: string
  awsProfile: string
  awsRegion: string
  ssmRunAs: string
  remotePort: string
  ttl: string
  remoteBin: string
}

// Defaults for a brand-new crew. The port and TTL mirror the backend's own
// defaults so an untouched form round-trips to the same record the API would
// have created on its own.
export const DEFAULT_REMOTE_PORT = '7777'
export const DEFAULT_TTL = '20h'
// The backend's own default for the remote account an SSM session runs as. A
// cleared field falls back to it rather than to the empty string, which the
// registry rejects for an SSM crew.
export const DEFAULT_SSM_RUN_AS = 'ec2-user'

export const EMPTY_INSTANCE_FORM: InstanceFormValues = {
  name: '',
  method: 'ssh',
  sshHost: '',
  ssmTarget: '',
  awsProfile: '',
  awsRegion: '',
  ssmRunAs: '',
  remotePort: DEFAULT_REMOTE_PORT,
  ttl: DEFAULT_TTL,
  remoteBin: '',
}

/** Seed the form from an existing crew, so editing starts from what is stored. */
export function instanceFormFromView(inst: InstanceView): InstanceFormValues {
  return {
    name: inst.name,
    method: inst.connection_method === 'ssm' ? 'ssm' : 'ssh',
    sshHost: inst.ssh_host || '',
    ssmTarget: inst.ssm_target || '',
    awsProfile: inst.aws_profile || '',
    awsRegion: inst.aws_region || '',
    ssmRunAs: inst.ssm_run_as || '',
    remotePort: String(inst.remote_port || DEFAULT_REMOTE_PORT),
    ttl: inst.ttl || DEFAULT_TTL,
    remoteBin: inst.remote_bin || '',
  }
}

/**
 * Form state plus everything derived from it that both forms need to gate their
 * submit button. `usedPorts` must exclude the crew being edited — its own port
 * is not a conflict with itself.
 */
export function useInstanceFormState(initial: InstanceFormValues, usedPorts: number[]) {
  const [values, setValues] = useState<InstanceFormValues>(initial)
  const set = useCallback(
    <K extends keyof InstanceFormValues>(key: K, value: InstanceFormValues[K]) => {
      setValues(prev => ({ ...prev, [key]: value }))
    },
    [],
  )
  const isSsm = values.method === 'ssm'
  const portNum = Number(values.remotePort) || 0
  const dupPort = portNum > 0 && usedPorts.includes(portNum)
  // The transport-specific required field: ssh_host for SSH, ssm_target for SSM.
  const targetFilled = isSsm ? !!values.ssmTarget.trim() : !!values.sshHost.trim()
  const valid = !!values.name.trim() && targetFilled && !dupPort
  /**
   * The request payload. Fields belonging to the transport that is NOT selected
   * are omitted rather than blanked: the backend validates them only for their
   * own transport, so leaving them intact means switching SSH → SSM → SSH does
   * not silently erase what the user typed earlier.
   *
   * `explicitClears` is what an EDIT needs. A create can omit an empty optional
   * and let the backend apply its default, but an update is a partial one: an
   * omitted key means "leave as-is", so emptying a field the user wants gone
   * would silently keep the old value — the crew would go on connecting through
   * an AWS profile that no longer appears anywhere in the form. Sending the
   * empty value makes the clear real, and makes it visible to the
   * transport-change check that decides whether to reopen the tunnel.
   */
  const body = useCallback(
    ({ explicitClears = false }: { explicitClears?: boolean } = {}): AddInstanceBody => {
      const v = values
      const ssm = v.method === 'ssm'
      const opt = (raw: string, cleared?: string) =>
        raw.trim() || (explicitClears ? cleared ?? '' : undefined)
      return {
        name: v.name.trim(),
        connection_method: v.method,
        ...(ssm
          ? {
              ssm_target: v.ssmTarget.trim(),
              aws_profile: opt(v.awsProfile),
              aws_region: opt(v.awsRegion),
              // An SSM crew must always name a remote user, so a cleared field
              // returns to the default rather than to the empty string.
              ssm_run_as: opt(v.ssmRunAs, DEFAULT_SSM_RUN_AS),
            }
          : { ssh_host: v.sshHost.trim() }),
        remote_port: Number(v.remotePort) || Number(DEFAULT_REMOTE_PORT),
        ttl: v.ttl.trim() || DEFAULT_TTL,
        remote_bin: opt(v.remoteBin),
      }
    },
    [values],
  )
  return { values, set, reset: setValues, isSsm, portNum, dupPort, targetFilled, valid, body }
}

export type InstanceFormState = ReturnType<typeof useInstanceFormState>

/** Fields whose value decides HOW the tunnel is opened, not just how it is labelled. */
const TRANSPORT_FIELDS = [
  'ssh_host',
  'remote_port',
  'connection_method',
  'ssm_target',
  'aws_profile',
  'aws_region',
  'ssm_run_as',
  'remote_bin',
] as const

/** Whether a saved change invalidates a tunnel that is already open. */
function changesTransport(before: InstanceView, body: AddInstanceBody): boolean {
  const next = body as unknown as Record<string, unknown>
  const current = before as unknown as Record<string, unknown>
  return TRANSPORT_FIELDS.some(f => next[f] !== undefined && next[f] !== current[f])
}

/**
 * Edit an already-configured crew in place. Editing preserves the crew's
 * identity; the only alternative is delete-and-re-add, which discards the record
 * along with the typo.
 *
 * A crew the user MEANT to be connected is reconnected here rather than left
 * down: the gateway tears any stale tunnel down as part of the save, so without
 * this the row would sit disconnected after an edit the user experienced as
 * "change the port". Connect intent, not live state, is the test — a crew that
 * is erroring is the likeliest reason someone opened this form. A crew that has
 * never been connected stays down: opening a tunnel and minting a remote token
 * is not something Save asked for.
 *
 * That intent is read off the PATCH RESPONSE, not off the row this form was
 * opened with. The row comes from a poll and can be seconds stale, so a
 * Disconnect pressed while the save was in flight would otherwise be undone by
 * a reconnect the user never asked for.
 */
export function EditInstanceForm({
  inst,
  usedPorts,
  onSaved,
  onCancel,
}: {
  inst: InstanceView
  /** Ports taken by OTHER crews — this crew's own port is not a conflict. */
  usedPorts: number[]
  onSaved: () => void
  onCancel: () => void
}) {
  const dispatch = useAppDispatch()
  const form = useInstanceFormState(instanceFormFromView(inst), usedPorts)
  const saveMutation = useMutation({
    mutationFn: async () => {
      const body = form.body({ explicitClears: true })
      const updated = await api.updateInstance(inst.id, body)
      // Only a transport change needs this; a rename must not perturb a healthy
      // connection.
      const intended = updated.was_connected || updated.status?.state === 'connected'
      if (intended && changesTransport(inst, body)) {
        const st = await api.connectInstance(inst.id)
        // The new tunnel has a new port and token. Without publishing them the
        // store keeps the pre-edit pair, so the pane iframe stays on the dead
        // origin AND a later selection sees a warm+connected crew and skips the
        // reconnect that would have healed it.
        if (st.state === 'connected' && st.local_port && st.token) {
          dispatch(setWarm({ id: inst.id, conn: { port: st.local_port, token: st.token } }))
        }
      }
      return updated
    },
    onSuccess: onSaved,
  })
  const err = saveMutation.error
    ? saveMutation.error instanceof ApiError
      ? saveMutation.error.message
      : i18nT('pages.settings.remoteCrewPanel.failed_to_save_crew')
    : ''
  return (
    <div
      className="mt-3 rounded-md border border-border bg-bg-elevated p-3"
      role="group"
      aria-label={i18nT('pages.settings.remoteCrewPanel.edit_crew', { name: inst.name })}
    >
      <div className="flex items-center gap-2 mb-3 text-text font-medium text-sm">
        <Pencil className="lucide-inline" />{' '}
        {i18nT('pages.settings.remoteCrewPanel.edit_crew', { name: inst.name })}
      </div>
      <InstanceFormFields idPrefix={`edit-instance-${inst.id}`} form={form} />
      <p className="mt-2 text-[12px] text-muted">
        {i18nT('pages.settings.remoteCrewPanel.edit_reconnect_note')}
      </p>
      <ErrorNotice message={err} className="mt-3" />
      <div className="mt-3 flex items-center gap-2">
        <Btn
          primary
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending || !form.valid}
        >
          {saveMutation.isPending
            ? i18nT('pages.settings.remoteCrewPanel.saving')
            : i18nT('pages.settings.remoteCrewPanel.save_changes')}
        </Btn>
        <Btn onClick={onCancel} disabled={saveMutation.isPending}>
          {i18nT('pages.settings.remoteCrewPanel.cancel')}
        </Btn>
      </div>
    </div>
  )
}

const inputCls =
  'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-none focus-ring'

export function InstanceFormFields({
  idPrefix,
  form,
}: {
  idPrefix: string
  form: InstanceFormState
}) {
  const { values, set, isSsm, portNum, dupPort } = form
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <label htmlFor={`${idPrefix}-name`} className="flex flex-col gap-1 text-[13px] text-muted">
        {i18nT('pages.settings.instancesPanel.name')}
        <input id={`${idPrefix}-name`} aria-label={i18nT('pages.settings.instancesPanel.name')} className={inputCls} value={values.name} onChange={e => set('name', e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.remote_host_1')} />
      </label>
      {/* Not a <label>: SimpleSelect renders a button, so `htmlFor` would point
          at no form control. The caption text stays put and the accessible name
          moves to the trigger's aria-label (same key). */}
      <div className="flex flex-col gap-1 text-[13px] text-muted">
        {i18nT('pages.settings.instancesPanel.connection_method')}
        <SimpleSelect
          options={['ssh', 'ssm']}
          optionLabels={[i18nT('pages.settings.instancesPanel.ssh_tunnel'), i18nT('pages.settings.instancesPanel.aws_ssm_session_manager')]}
          value={values.method}
          onChange={v => set('method', v as 'ssh' | 'ssm')}
          aria-label={i18nT('pages.settings.instancesPanel.connection_method')}
        />
        <span className="text-[12px] text-muted leading-snug">
          {isSsm
            ? i18nT('pages.settings.instancesPanel.tunnels_via_aws_ssm_start_session_no_inbound_ssh')
            : i18nT('pages.settings.instancesPanel.opens_ssh_n_l_to_the_host_requires_non_interacti')}
        </span>
      </div>
      {isSsm ? (
        <>
          <label htmlFor={`${idPrefix}-ssm-target`} className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.ssm_target_instance_id')}
            <input id={`${idPrefix}-ssm-target`} aria-label={i18nT('pages.settings.instancesPanel.ssm_target_instance_id')} className={inputCls} value={values.ssmTarget} onChange={e => set('ssmTarget', e.target.value)} placeholder="i-0123456789abcdef0" />
            <span className="text-[12px] text-muted leading-snug">
              {i18nT('pages.settings.instancesPanel.ec2_instance_id_i_or_ssm_managed_instance_id_mi')}
            </span>
          </label>
          <label htmlFor={`${idPrefix}-aws-profile`} className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.aws_profile')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
            <input id={`${idPrefix}-aws-profile`} aria-label={i18nT('pages.settings.instancesPanel.aws_profile')} className={inputCls} value={values.awsProfile} onChange={e => set('awsProfile', e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.default_credential_chain')} />
          </label>
          <label htmlFor={`${idPrefix}-aws-region`} className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.aws_region')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
            <input id={`${idPrefix}-aws-region`} aria-label={i18nT('pages.settings.instancesPanel.aws_region')} className={inputCls} value={values.awsRegion} onChange={e => set('awsRegion', e.target.value)} placeholder="us-east-1" />
          </label>
          <label htmlFor={`${idPrefix}-ssm-run-as`} className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.remote_user')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
            <input id={`${idPrefix}-ssm-run-as`} aria-label={i18nT('pages.settings.instancesPanel.remote_user')} className={inputCls} value={values.ssmRunAs} onChange={e => set('ssmRunAs', e.target.value)} placeholder="ec2-user" />
            <span className="text-[12px] text-muted leading-snug">
              {i18nT('pages.settings.instancesPanel.the_user_the_remote_gateway_runs_as_sudo_u_for_s')}
            </span>
          </label>
        </>
      ) : (
        <label htmlFor={`${idPrefix}-ssh-host`} className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.instancesPanel.ssh_host_alias')}
          <input id={`${idPrefix}-ssh-host`} aria-label={i18nT('pages.settings.instancesPanel.ssh_host_alias')} className={inputCls} value={values.sshHost} onChange={e => set('sshHost', e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.host_1_alias')} />
        </label>
      )}
      <label htmlFor={`${idPrefix}-remote-port`} className="flex flex-col gap-1 text-[13px] text-muted">
        {i18nT('pages.settings.instancesPanel.remote_port')}
        <input id={`${idPrefix}-remote-port`} aria-label={i18nT('pages.settings.instancesPanel.remote_port')} className={inputCls} value={values.remotePort} onChange={e => set('remotePort', e.target.value)} placeholder="7777" inputMode="numeric" />
        <span className="text-[12px] text-muted leading-snug">
          {i18nT('pages.settings.instancesPanel.must_match_the_port_the_remote_gateway_serves_on')}
        </span>
        {dupPort ? (
          <span className="text-[12px] text-danger leading-snug">
            {i18nT('pages.settings.instancesPanel.port')} {fmtNumber(portNum)} {i18nT('pages.settings.instancesPanel.is_already_used_by_another_instance_choose_a_dif')}
          </span>
        ) : null}
      </label>
      <label htmlFor={`${idPrefix}-ttl`} className="flex flex-col gap-1 text-[13px] text-muted">
        {i18nT('pages.settings.instancesPanel.token_ttl')}
        <input id={`${idPrefix}-ttl`} aria-label={i18nT('pages.settings.instancesPanel.token_ttl')} className={inputCls} value={values.ttl} onChange={e => set('ttl', e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.20h')} />
      </label>
      <label htmlFor={`${idPrefix}-remote-bin`} className="flex flex-col gap-1 text-[13px] text-muted sm:col-span-2">
        {i18nT('pages.settings.instancesPanel.remote_kirocrew_path')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
        <input
          id={`${idPrefix}-remote-bin`}
          aria-label={i18nT('pages.settings.instancesPanel.remote_kirocrew_path')}
          className={inputCls}
          value={values.remoteBin}
          onChange={e => set('remoteBin', e.target.value)}
          placeholder={i18nT('pages.settings.instancesPanel.home_you_local_bin_kirocrew_leave_blank_for_stan')}
        />
        <span className="text-[12px] text-muted leading-snug">
          {i18nT('pages.settings.instancesPanel.only_needed_if')} <code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew')}</code> {i18nT('pages.settings.instancesPanel.is_installed_somewhere_non_standard_on_the_remot')} <code className="text-text">{i18nT('pages.settings.instancesPanel.command_v_kirocrew')}</code>{' '}
          {i18nT('pages.settings.instancesPanel.commonly')} <code className="text-text">{i18nT('pages.settings.instancesPanel.local_bin_kirocrew')}</code>{i18nT('pages.settings.instancesPanel.use_an_absolute_path_no')} <code className="text-text">~</code>).
        </span>
      </label>
    </div>
  )
}
