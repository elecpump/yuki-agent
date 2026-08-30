import { Button } from "antd";
import { useAppStore } from "../../state/store";
import { ConnectionStatus } from "../shell/ConnectionStatus";
import { ConsoleTabs } from "./ConsoleTabs";

export function ConsoleDrawer() {
  const open = useAppStore((state) => state.consoleOpen);
  const toggle = useAppStore((state) => state.toggleConsole);
  return (
    <aside className={`console-drawer ${open ? "" : "closed"}`} aria-hidden={!open}>
      <div className="console-header">
        <strong>控制台</strong>
        <Button type="text" onClick={toggle}>收起</Button>
      </div>
      <div className="console-content"><ConsoleTabs /></div>
      <div className="console-footer"><ConnectionStatus /></div>
    </aside>
  );
}
