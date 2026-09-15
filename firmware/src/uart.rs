use core::convert::Infallible;

use embassy_futures::select::{select3, Either3};
use embassy_rp::{
    peripherals::PIO1,
    usb::{self},
};
use embassy_usb::{
    class::cdc_acm::{CdcAcmClass, ControlChanged, Receiver, Sender},
    driver::EndpointError,
};
use embedded_io_async::{Read, Write};

use crate::pio_uart::PioUart;

pub enum UartTaskError {
    Disconnected,
}

impl From<EndpointError> for UartTaskError {
    fn from(val: EndpointError) -> Self {
        match val {
            EndpointError::BufferOverflow => panic!("Buffer overflow"),
            EndpointError::Disabled => UartTaskError::Disconnected {},
        }
    }
}

impl From<Infallible> for UartTaskError {
    fn from(val: Infallible) -> Self {
        match val {}
    }
}

#[embassy_executor::task]
pub async fn usb_task(class: CdcAcmClass<'static, super::UsbDriver>, mut uart: PioUart<'static, PIO1>) -> ! {
    let (mut tx, mut rx, mut ctrl) = class.split_with_control();

    loop {
        rx.wait_connection().await;
        // Apply baud rate immediately after connection, before entering the
        // forwarding loop. The host sends SET_LINE_CODING during port open,
        // which control_changed() inside pipe_uart would miss.
        let baudrate = rx.line_coding().data_rate();
        if baudrate > 0 {
            uart.set_baudrate(baudrate);
        }
        let _ = pipe_uart(&mut tx, &mut rx, &mut ctrl, &mut uart).await;
    }
}

/// Handle ASIC UART <-> USB CDC forwarding and baudrate changes
pub async fn pipe_uart<'d, T: usb::Instance + 'd>(
    usb_tx: &mut Sender<'d, usb::Driver<'d, T>>,
    usb_rx: &mut Receiver<'d, usb::Driver<'d, T>>,
    ctrl: &mut ControlChanged<'d>,
    uart: &mut PioUart<'static, PIO1>,
) -> Result<(), UartTaskError> {
    let mut usb_buf = [0u8; 64];
    let mut uart_buf = [0u8; 1024];

    loop {
        let (uart_tx, uart_rx) = uart.split_ref();
        let usb_read = usb_rx.read_packet(&mut usb_buf);
        let uart_read = uart_rx.read(&mut uart_buf);
        let control_change = ctrl.control_changed();

        match select3(usb_read, uart_read, control_change).await {
            // Forward data from the USB host to the UART
            Either3::First(n) => {
                uart_tx.write_all(&usb_buf[..n?]).await?;
            }
            // Forward data from the UART back to the USB host
            Either3::Second(n) => {
                usb_tx.write_packet(&uart_buf[..n?]).await?;
            }
            // Handle baudrate changes from USB CDC control requests
            Either3::Third(()) => {
                let baudrate = usb_rx.line_coding().data_rate();
                uart.set_baudrate(baudrate);
            }
        }
    }
}
