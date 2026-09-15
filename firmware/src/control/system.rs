use super::CommandError;
use heapless::Vec;

/// Control page 0x0F (SYSTEM):
///   [0x00]              -> firmware identity string (confirms which build a board runs)
///   [0x01, 0xB0, 0x07]  -> ack [0x01], then reboot into the RP2040 ROM USB bootloader (RPI-RP2 drive)
/// The 2-byte key keeps a stray or garbled control packet from dropping the board into BOOTSEL.
pub const FW_IDENT: &str = "ember-one kf1950 pio96 +sys-cmds 2026-09-15";

#[derive(defmt::Format)]
pub enum Command {
    Identify,
    RebootToBootloader,
}

impl Command {
    pub fn from_bytes(buf: &[u8]) -> Result<Self, CommandError> {
        match buf {
            [0x00] => Ok(Self::Identify),
            [0x01, 0xB0, 0x07] => Ok(Self::RebootToBootloader),
            _ => Err(CommandError::Invalid),
        }
    }
}

impl super::ControllerCommand for Command {
    async fn handle(&self, controller: &mut super::Controller) -> Result<Vec<u8, 256>, CommandError> {
        match self {
            Command::Identify => Ok(Vec::from_slice(FW_IDENT.as_bytes()).unwrap()),
            Command::RebootToBootloader => {
                // Deferred: Controller::run sends this ack first, then reboots.
                controller.reboot_to_bootloader = true;
                Ok(Vec::from_slice(&[0x01]).unwrap())
            }
        }
    }
}
