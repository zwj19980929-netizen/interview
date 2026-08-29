export async function createAppointmentAndInvite({ createAppointment, issueInvitation }) {
  const appointment = await createAppointment();
  try {
    const invitation = await issueInvitation(appointment);
    return { appointment, invitation, inviteError: null };
  } catch (inviteError) {
    return { appointment, invitation: null, inviteError };
  }
}
