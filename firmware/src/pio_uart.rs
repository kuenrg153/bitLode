use core::convert::Infallible;

use embassy_rp::{
    clocks::clk_sys_freq,
    gpio::Level,
    pio::{Common, Config, Direction as PioDirection, FifoJoin, Instance, LoadedProgram, PioPin, ShiftDirection, StateMachine},
};
use embedded_io_async::{ErrorType, Read, Write};
use fixed::traits::ToFixed;

pub struct PioUartTx<'d, PIO: Instance, const SM: usize> {
    sm: StateMachine<'d, PIO, SM>,
}

impl<'d, PIO: Instance, const SM: usize> PioUartTx<'d, PIO, SM> {
    fn new(
        baud: u32,
        common: &mut Common<'d, PIO>,
        mut sm: StateMachine<'d, PIO, SM>,
        tx_pin: impl PioPin,
        prg: &LoadedProgram<'d, PIO>,
    ) -> Self {
        let tx_pin = common.make_pio_pin(tx_pin);
        sm.set_pins(Level::High, &[&tx_pin]);
        sm.set_pin_dirs(PioDirection::Out, &[&tx_pin]);

        let mut cfg = Config::default();
        cfg.set_out_pins(&[&tx_pin]);
        cfg.use_program(prg, &[&tx_pin]);
        cfg.shift_out.auto_fill = false;
        cfg.shift_out.direction = ShiftDirection::Right;
        cfg.fifo_join = FifoJoin::TxOnly;
        cfg.clock_divider = (clk_sys_freq() / (8 * baud)).to_fixed();
        sm.set_config(&cfg);
        sm.set_enable(true);

        Self { sm }
    }

    fn set_baudrate(&mut self, baud: u32) {
        self.sm.set_clock_divider((clk_sys_freq() / (8 * baud)).to_fixed());
        self.sm.clkdiv_restart();
    }
}

impl<PIO: Instance, const SM: usize> ErrorType for PioUartTx<'_, PIO, SM> {
    type Error = Infallible;
}

impl<PIO: Instance, const SM: usize> Write for PioUartTx<'_, PIO, SM> {
    async fn write(&mut self, buf: &[u8]) -> Result<usize, Infallible> {
        for &byte in buf {
            self.sm.tx().wait_push(byte as u32).await;
        }
        Ok(buf.len())
    }
}

pub struct PioUartRx<'d, PIO: Instance, const SM: usize> {
    sm: StateMachine<'d, PIO, SM>,
}

impl<'d, PIO: Instance, const SM: usize> PioUartRx<'d, PIO, SM> {
    fn new(
        baud: u32,
        common: &mut Common<'d, PIO>,
        mut sm: StateMachine<'d, PIO, SM>,
        rx_pin: impl PioPin,
        prg: &LoadedProgram<'d, PIO>,
    ) -> Self {
        let rx_pin = common.make_pio_pin(rx_pin);
        sm.set_pins(Level::High, &[&rx_pin]);
        sm.set_pin_dirs(PioDirection::In, &[&rx_pin]);

        let mut cfg = Config::default();
        cfg.use_program(prg, &[]);
        cfg.set_in_pins(&[&rx_pin]);
        cfg.set_jmp_pin(&rx_pin);
        cfg.shift_in.auto_fill = false;
        cfg.shift_in.direction = ShiftDirection::Right;
        cfg.shift_in.threshold = 32;
        cfg.fifo_join = FifoJoin::RxOnly;
        cfg.clock_divider = (clk_sys_freq() / (8 * baud)).to_fixed();
        sm.set_config(&cfg);
        sm.set_enable(true);

        Self { sm }
    }

    fn set_baudrate(&mut self, baud: u32) {
        self.sm.set_clock_divider((clk_sys_freq() / (8 * baud)).to_fixed());
        self.sm.clkdiv_restart();
    }
}

impl<PIO: Instance, const SM: usize> ErrorType for PioUartRx<'_, PIO, SM> {
    type Error = Infallible;
}

impl<PIO: Instance, const SM: usize> Read for PioUartRx<'_, PIO, SM> {
    async fn read(&mut self, buf: &mut [u8]) -> Result<usize, Infallible> {
        if buf.is_empty() {
            return Ok(0);
        }
        // Block until at least one byte is available
        buf[0] = self.sm.rx().wait_pull().await as u8;
        // Drain any additional bytes already waiting in the FIFO
        let mut count = 1;
        while count < buf.len() {
            match self.sm.rx().try_pull() {
                Some(v) => { buf[count] = v as u8; count += 1; }
                None => break,
            }
        }
        Ok(count)
    }
}

pub struct PioUart<'d, PIO: Instance> {
    // Kept alive to prevent instruction memory slots being marked free
    _tx_prg: LoadedProgram<'d, PIO>,
    _rx_prg: LoadedProgram<'d, PIO>,
    pub tx: PioUartTx<'d, PIO, 0>,
    pub rx: PioUartRx<'d, PIO, 1>,
}

impl<'d, PIO: Instance> PioUart<'d, PIO> {
    pub fn new(
        baud: u32,
        common: &mut Common<'d, PIO>,
        sm_tx: StateMachine<'d, PIO, 0>,
        sm_rx: StateMachine<'d, PIO, 1>,
        tx_pin: impl PioPin,
        rx_pin: impl PioPin,
    ) -> Self {
        let tx_prog = pio_proc::pio_asm!(
            r#"
                .side_set 1 opt

                    pull       side 1 [7]
                    set x, 7   side 0 [7]
                bitloop:
                    out pins, 1
                    jmp x-- bitloop   [6]
            "#
        );
        let rx_prog = pio_proc::pio_asm!(
            r#"
                start:
                    wait 0 pin 0
                    set x, 7    [10]
                rx_bitloop:
                    in pins, 1
                    jmp x-- rx_bitloop [6]
                    jmp pin good_stop
                    irq 4 rel
                    wait 1 pin 0
                    jmp start
                good_stop:
                    in null 24
                    push
            "#
        );

        let tx_prg = common.load_program(&tx_prog.program);
        let rx_prg = common.load_program(&rx_prog.program);

        let tx = PioUartTx::new(baud, common, sm_tx, tx_pin, &tx_prg);
        let rx = PioUartRx::new(baud, common, sm_rx, rx_pin, &rx_prg);

        Self { _tx_prg: tx_prg, _rx_prg: rx_prg, tx, rx }
    }

    pub fn split_ref(&mut self) -> (&mut PioUartTx<'d, PIO, 0>, &mut PioUartRx<'d, PIO, 1>) {
        (&mut self.tx, &mut self.rx)
    }

    pub fn set_baudrate(&mut self, baud: u32) {
        self.tx.set_baudrate(baud);
        self.rx.set_baudrate(baud);
    }
}
